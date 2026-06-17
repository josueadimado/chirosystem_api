from __future__ import annotations

import mimetypes
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache
from django.core.signing import TimestampSigner
from django.db import transaction
from django.db.models import Case, Count, IntegerField, OuterRef, Prefetch, Q, Subquery, Sum, Value, When
from django.db.models.functions import TruncDate
from django.http import FileResponse, Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import mixins, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from .models import (
    Appointment,
    ClinicSettings,
    DiagnosisCode,
    Invoice,
    Patient,
    PatientCreditTransaction,
    PatientDocument,
    Payment,
    Provider,
    ProviderUnavailability,
    Service,
    StaffNotification,
    Visit,
    VisitRenderedService,
    VoiceCallLog,
)
from .public_booking_service import (
    _appointment_start_aware_in_clinic_tz,
    cancel_appointment_public,
    create_appointment_from_public_booking,
    public_online_booking_calendar_span_minutes,
    reschedule_appointment_public,
)
from .utils import format_time_12h, normalize_phone, validate_phone
from .serializers import (
    AppointmentHandoffNotesSerializer,
    AppointmentSoapNotesSerializer,
    AppointmentListSerializer,
    AppointmentSerializer,
    ClinicProfileUpdateSerializer,
    DoctorCompleteVisitSerializer,
    InvoiceSerializer,
    InvoiceApplyCreditSerializer,
    PatientIntakeUpdateSerializer,
    PatientCreditTopUpSerializer,
    PatientCreditTransactionSerializer,
    PatientListSerializer,
    PatientSerializer,
    SaveSquareCardSerializer,
    StaffSavePatientCardSerializer,
    TerminalCheckoutSerializer,
    TerminalCheckoutStatusSerializer,
    TerminalCheckoutTestSerializer,
    PaymentCompleteSerializer,
    PaymentSerializer,
    PublicBookingSerializer,
    RecurringBookingPreviewSerializer,
    RecurringBookingSerializer,
    DeskRecurringBookingPreviewSerializer,
    DeskRecurringBookingSerializer,
    PublicCancelSerializer,
    PublicRescheduleSerializer,
    ProviderSerializer,
    ProviderUnavailabilityBulkSerializer,
    ProviderUnavailabilitySerializer,
    DiagnosisCodeSerializer,
    ServiceSerializer,
    StaffNotificationSerializer,
    VisitCompleteSerializer,
    VisitSerializer,
    VoiceCallLogSerializer,
    complete_visit_with_services,
    revise_unpaid_visit_billing,
    revise_visit_billing_admin,
)
from .analytics import build_admin_analytics_payload, parse_analytics_months
from .doctor_analytics import build_doctor_my_analytics_payload, parse_analytics_weeks
from .pagination import StandardPageNumberPagination
from .square_helpers import (
    get_application_id,
    get_location_id,
    get_terminal_device_id,
    save_card_from_source,
    square_configured,
)
from .google_calendar_sync import (
    build_oauth_flow,
    exchange_oauth_code,
    google_oauth_configured,
)
from .patient_communication_prefs import patient_communication_prefs_payload
from .patient_demographics import (
    annotate_patient_list_stats,
    annotate_patient_unpaid_balances,
    apply_patient_intake_validated_data,
    apply_patient_directory_list_filter,
    patient_account_summary,
    patient_demographics_summary,
)
from .doctor_dashboard_appointments import (
    appointments_for_doctor_dashboard,
    serialize_doctor_dashboard_appointments,
)
from .patient_book_next_context import book_next_context_for_appointment
from .patient_prior_diagnoses import (
    consultation_diagnosis_prefill_for_appointment,
    consultation_workspace_for_appointment,
)
from .visit_diagnosis import diagnosis_ids_from_visit, serialize_visit_diagnoses
from .provider_patient_access import (
    appointment_matches_provider_discipline,
    clinical_access_level,
    clinical_access_message,
    filter_patient_queryset_for_provider_discipline,
    provider_for_doctor_user,
)
from .square_pos import (
    build_android_square_pos_intent,
    build_ios_square_pos_url,
    pos_callback_configured,
)
from .square_payment import (
    build_invoice_payment_followup_dict,
    create_payment_link_for_credit_topup,
    create_terminal_checkout_for_invoice,
    create_terminal_checkout_for_credit_topup,
    create_terminal_checkout_test,
    get_frontend_base_url,
    get_terminal_checkout_status,
    try_push_terminal_checkout_to_kiosk,
    reconcile_open_invoices_for_patient,
    staff_confirm_invoice_paid,
    sync_invoice_payment_from_square,
    try_reconcile_invoice_from_square,
)
from .booking_availability import provider_interval_blocked_online
from .booking_provider_eligibility import apply_intake_chiropractic_provider_fallback, provider_can_offer_service_online

# Optional Square / card-on-file fields — defer on read-heavy querysets so SELECT does not
# reference missing columns if migrations have not been applied yet.
_PATIENT_OPTIONAL_CARD_FIELDS = (
    "square_customer_id",
    "square_card_id",
    "card_brand",
    "card_last4",
)


def _defer_patient_card_fields(qs, *, patient_prefix: str | None = None):
    """
    Omit optional payment columns from SQL (nested patient FK: use patient_prefix e.g. 'patient').
    """
    if patient_prefix:
        names = [f"{patient_prefix}__{f}" for f in _PATIENT_OPTIONAL_CARD_FIELDS]
    else:
        names = list(_PATIENT_OPTIONAL_CARD_FIELDS)
    return qs.defer(*names)


def _clinic_settings_bill_header():
    """Header fields for printed bills and API responses (single DB row, served from cache)."""
    s = ClinicSettings.get_cached()
    return {
        "clinic_name": s.clinic_name,
        "address_line1": s.address_line1,
        "city_state_zip": s.city_state_zip,
        "phone": s.phone,
        "email": s.email or "",
        "employer_tax_id": (s.employer_tax_id or "").strip(),
        "provider_billing_id": (s.provider_billing_id or "").strip(),
        "pos_default": s.pos_default,
        "timezone": (s.timezone or "America/Detroit").strip(),
    }


def _bill_provider_id_display(inv: Invoice, header: dict) -> str:
    """Provider ID on patient bills: per-doctor override, else clinic setting, else legacy employer ID."""
    prov = inv.appointment.provider if inv.appointment_id else None
    if prov is not None:
        per = (getattr(prov, "billing_provider_id", None) or "").strip()
        if per:
            return per
    clinic_id = (header.get("provider_billing_id") or header.get("employer_tax_id") or "").strip()
    return clinic_id


def _format_bill_display_date(d) -> str:
    """Human-readable date for printed bills (e.g. 'Aug 24, 2025')."""
    from datetime import date, datetime

    if d is None:
        return ""
    if isinstance(d, datetime):
        d = d.date()
    if not isinstance(d, date):
        return str(d)
    return f"{d.strftime('%b')} {d.day}, {d.year}"


def _serialize_billing_invoice_row(inv: Invoice) -> dict:
    """One row for GET /admin/billing_invoices/ (list + detail modal)."""
    row = {
        "id": inv.id,
        "invoice_number": inv.invoice_number,
        "patient_id": inv.patient_id,
        "patient_name": f"{inv.patient.first_name} {inv.patient.last_name}",
        "patient_payment_profile": (inv.patient.payment_profile or "").strip(),
        "patient_credit_balance": str(inv.patient.credit_balance),
        "status": inv.status,
        "kind": inv.kind,
        "appointment_id": inv.appointment_id,
        "appointment_status": inv.appointment.status if inv.appointment_id else None,
        "appointment_date": str(inv.appointment.appointment_date) if inv.appointment_id else None,
        "booked_service_id": inv.appointment.booked_service_id if inv.appointment_id else None,
        "total_amount": str(inv.total_amount),
        "subtotal": str(inv.subtotal),
        "discount": str(inv.discount),
        "credit_applied_total": str(inv.credit_applied_total),
        "professional_discount_reason": inv.professional_discount_reason or "",
        "tax": str(inv.tax),
        "issued_at": inv.issued_at.isoformat() if inv.issued_at else None,
        "paid_at": inv.paid_at.isoformat() if inv.paid_at else None,
    }
    if inv.visit_id:
        row.update(_visit_invoice_bill_totals(inv.visit, inv))
    else:
        cached_payments = getattr(inv, "_successful_payments", None)
        if cached_payments is not None:
            pay_sum = sum((p.amount for p in cached_payments), Decimal("0"))
        else:
            pay_sum = inv.payments.filter(status=Payment.Status.SUCCESSFUL).aggregate(s=Sum("amount"))["s"]
            pay_sum = pay_sum if pay_sum is not None else Decimal("0")
        payments_received = (pay_sum + inv.credit_applied_total).quantize(Decimal("0.01"))
        patient_charge = inv.total_amount.quantize(Decimal("0.01"))
        remaining_client = (patient_charge - payments_received).quantize(Decimal("0.01"))
        if remaining_client < Decimal("0"):
            remaining_client = Decimal("0.00")
        row.update(
            {
                "bill_charges_total": str(inv.subtotal),
                "patient_charge_total": str(patient_charge),
                "insurance_remaining_total": "0.00",
                "payments_received_total": str(payments_received),
                "remaining_client_responsibility_total": str(remaining_client),
            }
        )
    return row


def _visit_invoice_bill_totals(visit: Visit, inv: Invoice) -> dict:
    """
    Split documented visit charges into patient (Relief Chiropractic) vs insurance-only lines.
    patient_charge_total = invoice total the client pays at the clinic (after discount).
    insurance_remaining_total = services documented for insurance, not charged to the patient.

    Aggregations are computed in Python to reuse any prefetched rendered_services / payments
    caches, avoiding extra DB queries inside list loops.
    """
    all_lines = list(visit.rendered_services.all())
    documented = sum((rs.total_price for rs in all_lines), Decimal("0")) or inv.subtotal
    documented = documented.quantize(Decimal("0.01"))

    insurance = sum(
        (rs.total_price for rs in all_lines if not rs.charges_patient), Decimal("0")
    )
    insurance = insurance.quantize(Decimal("0.01"))

    # Use prefetch_related to_attr cache when available, otherwise fall back to DB.
    cached_payments = getattr(inv, "_successful_payments", None)
    if cached_payments is not None:
        pay_sum = sum((p.amount for p in cached_payments), Decimal("0"))
    else:
        pay_sum = inv.payments.filter(status=Payment.Status.SUCCESSFUL).aggregate(s=Sum("amount"))["s"]
        pay_sum = pay_sum if pay_sum is not None else Decimal("0")
    payments_received = (pay_sum + inv.credit_applied_total).quantize(Decimal("0.01"))
    patient_charge = inv.total_amount.quantize(Decimal("0.01"))
    remaining_client = (patient_charge - payments_received).quantize(Decimal("0.01"))
    if remaining_client < Decimal("0"):
        remaining_client = Decimal("0.00")

    return {
        "bill_charges_total": str(documented),
        "patient_charge_total": str(patient_charge),
        "insurance_remaining_total": str(insurance),
        "payments_received_total": str(payments_received),
        "remaining_client_responsibility_total": str(remaining_client),
        "payments_card_cash_total": str(pay_sum.quantize(Decimal("0.01"))),
    }


def _invoice_bill_dict(inv: Invoice, *, preview: bool) -> dict:
    """Shared JSON for printable / preview patient bill — always reads live visit lines and invoice totals from the DB."""
    visit = inv.visit
    header = _clinic_settings_bill_header()
    lines = [
        _printable_invoice_line(rs, header["pos_default"])
        for rs in visit.rendered_services.all().order_by("id")
    ]
    pat = inv.patient
    addr_display = pat.city_state_zip or "St Joseph, MI 49085"
    if pat.address_line1:
        addr_display = ", ".join(filter(None, [pat.address_line1, pat.city_state_zip])) or addr_display
    totals = _visit_invoice_bill_totals(visit, inv)

    billing_anchor = inv.paid_at.date() if inv.paid_at else inv.appointment.appointment_date

    return {
        **header,
        "bill_title": "Patient Bill — PREVIEW (not paid yet)" if preview else "Patient Bill",
        "is_preview": preview,
        "invoice_id": inv.pk,
        "invoice_number": inv.invoice_number,
        "patient_id": pat.pk,
        "date_of_service": str(inv.appointment.appointment_date),
        "billing_date_display": _format_bill_display_date(billing_anchor),
        "statement_date_display": _format_bill_display_date(timezone.localdate()),
        "patient_name": f"{pat.first_name} {pat.last_name}",
        "patient_payment_profile": (pat.payment_profile or "").strip(),
        "patient_address": addr_display,
        "diagnosis": (visit.diagnosis or "").strip() or "\u2014",
        "provider_name": str(inv.appointment.provider) if inv.appointment and inv.appointment.provider else "",
        "provider_credential": (
            (inv.appointment.provider.credential or inv.appointment.provider.title or "").strip()
            if inv.appointment and inv.appointment.provider
            else ""
        ),
        "provider_billing_id": _bill_provider_id_display(inv, header),
        "lines": lines,
        # Bill Charges row = all documented line amounts (patient + insurance-only).
        "subtotal": totals["bill_charges_total"],
        "patient_subtotal": str(inv.subtotal),
        "discount": str(inv.discount),
        "credit_applied_total": str(inv.credit_applied_total),
        **totals,
        # Back-compat: patient_payments_total on printed bill = clinic charge (not card/cash received).
        "patient_payments_total": totals["patient_charge_total"],
        "insurance_payments_total": totals["insurance_remaining_total"],
        "tax": str(inv.tax),
        "total_amount": str(inv.total_amount),
        "status": inv.status,
    }


def _doctor_collect_payment_followup(invoice: Invoice, *, try_saved_card: bool) -> dict:
    """Payment banner payload: Square options plus other open penalty balances."""
    from .patient_payment_pending import build_doctor_pending_payment_context

    followup = build_invoice_payment_followup_dict(invoice, try_saved_card=try_saved_card)
    followup.pop("already_paid", None)
    followup["pending_payment"] = build_doctor_pending_payment_context(
        invoice.patient_id,
        current_invoice_id=invoice.id,
    )
    return followup


def _set_appointment_status_after_invoice_paid(inv: Invoice) -> None:
    from apps.clinic.invoice_collection import set_appointment_status_after_invoice_paid

    set_appointment_status_after_invoice_paid(inv)


def _invoice_bill_access_ok_for_preview(inv: Invoice) -> bool:
    """Invoice states where a bill PDF/HTML can be generated (unpaid preview or paid final)."""
    return inv.status in (
        Invoice.Status.ISSUED,
        Invoice.Status.OVERDUE,
        Invoice.Status.PAID,
    )


def _invoice_bill_preview_requested(request) -> bool:
    v = (request.query_params.get("preview") or "").strip().lower()
    return v in ("1", "true", "yes")


def _invoice_for_bill_email(invoice_id):
    return (
        Invoice.objects.select_related("patient", "appointment__provider", "visit")
        .prefetch_related("visit__rendered_services__service")
        .filter(pk=invoice_id)
        .first()
    )


def _email_patient_bill_response(request, *, provider=None):
    """POST body: { invoice_id }. Emails paid bill to patient email on file."""
    from apps.clinic.patient_bill_email import PatientBillEmailError, send_patient_bill_email

    raw = request.data.get("invoice_id")
    if raw is None:
        return Response({"detail": "invoice_id is required."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        invoice_id = int(raw)
    except (TypeError, ValueError):
        return Response({"detail": "invoice_id must be a number."}, status=status.HTTP_400_BAD_REQUEST)

    inv = _invoice_for_bill_email(invoice_id)
    if not inv:
        return Response({"detail": "Invoice not found."}, status=status.HTTP_404_NOT_FOUND)

    if provider is not None:
        if not inv.appointment_id or inv.appointment.provider_id != provider.id:
            return Response(
                {"detail": "You can only email bills for appointments on your schedule."},
                status=status.HTTP_403_FORBIDDEN,
            )

    try:
        bill = _invoice_bill_dict(inv, preview=False)
        recipient = send_patient_bill_email(inv, bill)
    except PatientBillEmailError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    return Response(
        {
            "detail": f"Patient bill emailed to {recipient}.",
            "recipient": recipient,
        },
        status=status.HTTP_200_OK,
    )


def _printable_invoice_line(rs, pos_default):
    """Bill table row: fees and line_total are documented amounts for every rendered service.

    patient_due is the amount that counts toward the patient's balance (0 when charges_patient is False).
    Invoice subtotal/total_amount exclude insurance-only lines; printed description stays the service text only.
    """
    svc = rs.service
    desc = (svc.description or svc.name)[:220]
    patient_due = str(rs.total_price) if rs.charges_patient else "0.00"
    return {
        "service_offered": svc.name,
        "cpt_code": svc.billing_code or "\u2014",
        "description": desc,
        "fees": str(rs.unit_price),
        "units": str(rs.quantity),
        "pos": pos_default,
        "line_total": str(rs.total_price),
        "patient_due": patient_due,
        "charges_patient": rs.charges_patient,
    }


def _can_edit_handoff_notes(request, appointment: Appointment, *, force_read_only: bool = False) -> bool:
    if force_read_only:
        return False
    role = getattr(request.user, "role", None)
    if role in ("owner_admin", "staff"):
        return True
    if role == "doctor":
        prov = provider_for_doctor_user(request.user)
        if not prov or appointment.provider_id != prov.id:
            return False
        if not appointment_matches_provider_discipline(appointment, prov):
            return False
        return clinical_access_level(prov, appointment.patient) == "full"
    return False


def _visit_billing_diagnosis_payload(visit: Visit) -> dict:
    return {
        "diagnosis": visit.diagnosis or "",
        "diagnoses": serialize_visit_diagnoses(visit),
        "diagnosis_ids": diagnosis_ids_from_visit(visit),
    }


def _visit_billing_for_edit_payload(appointment: Appointment, *, provider_id: int | None = None):
    """Load visit + invoice for billing editor; optional provider_id scopes to that doctor's appointments."""
    from .invoice_collection import invoice_payment_summary

    if provider_id is not None and appointment.provider_id != provider_id:
        return None, Response({"detail": "Appointment not found."}, status=status.HTTP_404_NOT_FOUND)
    if appointment.status not in (
        Appointment.Status.AWAITING_PAYMENT,
        Appointment.Status.COMPLETED,
    ):
        return None, Response(
            {"detail": "Billing can only be edited for completed visits or visits awaiting payment."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    visit = (
        Visit.objects.filter(appointment=appointment)
        .prefetch_related(
            Prefetch(
                "rendered_services",
                queryset=VisitRenderedService.objects.select_related("service").order_by("id"),
            ),
            "visit_diagnoses",
        )
        .first()
    )
    if not visit:
        return None, Response({"detail": "Visit not found."}, status=status.HTTP_404_NOT_FOUND)
    invoice = Invoice.objects.filter(appointment=appointment, visit=visit).first()
    if not invoice:
        return None, Response({"detail": "No invoice for this visit."}, status=status.HTTP_404_NOT_FOUND)
    if invoice.kind != Invoice.Kind.VISIT:
        return None, Response(
            {"detail": "Only normal visit invoices can be revised here."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if invoice.status == Invoice.Status.VOID:
        return None, Response({"detail": "This invoice is void."}, status=status.HTTP_400_BAD_REQUEST)
    if invoice.status not in (
        Invoice.Status.ISSUED,
        Invoice.Status.OVERDUE,
        Invoice.Status.DRAFT,
        Invoice.Status.PAID,
    ):
        return None, Response(
            {"detail": "Cannot edit billing for this invoice in its current state."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    rendered = [
        {
            "service_id": rs.service_id,
            "quantity": rs.quantity,
            "unit_price": str(rs.unit_price),
        }
        for rs in visit.rendered_services.all()
    ]
    return {
        "doctor_notes": visit.doctor_notes or "",
        **_visit_billing_diagnosis_payload(visit),
        "rendered_services": rendered,
        "invoice_id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "invoice_status": invoice.status,
        "discount": str(invoice.discount),
        "professional_discount_reason": invoice.professional_discount_reason or "",
        "total_amount": str(invoice.total_amount),
        **invoice_payment_summary(invoice),
    }, None


def _prepare_admin_uncancel_cancelled_appointment(appointment: Appointment) -> None:
    """Validate and prepare a cancelled appointment to be restored to booked (staff only)."""
    from apps.clinic.clinic_time import slot_start_is_in_past
    from apps.clinic.invoice_collection import invoice_payment_summary

    if slot_start_is_in_past(appointment.appointment_date, appointment.start_time):
        raise ValidationError(
            {
                "detail": (
                    "This appointment time has already passed. Book a new visit instead of restoring this one."
                )
            }
        )

    overlapping = (
        Appointment.objects.filter(
            provider_id=appointment.provider_id,
            appointment_date=appointment.appointment_date,
            start_time__lt=appointment.end_time,
            end_time__gt=appointment.start_time,
        )
        .exclude(pk=appointment.pk)
        .exclude(
            status__in=[
                Appointment.Status.CANCELLED,
                Appointment.Status.NO_SHOW,
                Appointment.Status.COMPLETED,
            ]
        )
        .exists()
    )
    if overlapping:
        raise ValidationError(
            {
                "detail": (
                    "That time slot is already booked for this provider. "
                    "Free the slot or reschedule before restoring this visit."
                )
            }
        )

    inv = (
        Invoice.objects.filter(
            appointment=appointment,
            kind=Invoice.Kind.LATE_CANCEL_FEE,
        )
        .order_by("-created_at")
        .first()
    )
    if not inv:
        return
    if inv.status == Invoice.Status.PAID:
        raise ValidationError(
            {
                "detail": (
                    "A late cancellation fee was already paid for this visit. "
                    "Book a new appointment instead of restoring this one."
                )
            }
        )
    paid = Decimal(str(invoice_payment_summary(inv).get("amount_paid", "0") or "0"))
    if paid > Decimal("0.01"):
        raise ValidationError(
            {
                "detail": (
                    "Payments were recorded on the late cancellation fee. "
                    "Resolve billing before restoring this visit."
                )
            }
        )
    if inv.status != Invoice.Status.VOID:
        inv.status = Invoice.Status.VOID
        inv.save(update_fields=["status", "updated_at"])


def _complete_visit_payload_from_validated(data: dict, rendered_payload: list) -> dict:
    payload = {
        "doctor_notes": data.get("doctor_notes", ""),
        "rendered_services": rendered_payload,
        "professional_discount": str(data.get("professional_discount", Decimal("0"))),
        "professional_discount_reason": data.get("professional_discount_reason", ""),
    }
    if "diagnosis_ids" in data:
        payload["diagnosis_ids"] = data["diagnosis_ids"]
    else:
        payload["diagnosis"] = data.get("diagnosis", "")
    return payload


def _truthy_request_flag(value) -> bool:
    """Parse JSON booleans or string flags like ``"true"`` / ``"1"`` from request bodies."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes")


def _parse_invoice_id_from_body(request) -> tuple[int | None, Response | None]:
    raw = request.data.get("invoice_id")
    if raw is None:
        return None, Response({"detail": "invoice_id is required."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        return int(raw), None
    except (TypeError, ValueError):
        return None, Response({"detail": "Invalid invoice_id."}, status=status.HTTP_400_BAD_REQUEST)


def _sync_invoice_payment_api_response(request) -> Response:
    invoice_id, err = _parse_invoice_id_from_body(request)
    if err:
        return err
    inv = Invoice.objects.filter(pk=invoice_id).select_related("appointment", "patient").first()
    if not inv:
        return Response({"detail": "Invoice not found."}, status=status.HTTP_404_NOT_FOUND)
    return Response(sync_invoice_payment_from_square(inv))


def _confirm_invoice_paid_api_response(request) -> Response:
    """Staff: mark paid when Square app shows payment but automatic sync cannot match it."""
    if getattr(request.user, "role", None) not in ("owner_admin", "staff"):
        return Response(
            {"detail": "Only clinic owner or staff can confirm a Square payment manually."},
            status=status.HTTP_403_FORBIDDEN,
        )
    invoice_id, err = _parse_invoice_id_from_body(request)
    if err:
        return err
    inv = Invoice.objects.filter(pk=invoice_id).select_related("appointment", "patient").first()
    if not inv:
        return Response({"detail": "Invoice not found."}, status=status.HTTP_404_NOT_FOUND)
    if inv.status == Invoice.Status.PAID:
        return Response({"ok": True, "paid": True, "detail": "Invoice is already marked paid."})
    confirm_no = (request.data.get("invoice_number") or "").strip()
    if confirm_no and confirm_no != inv.invoice_number:
        return Response(
            {"detail": "Invoice number does not match. Open the correct bill and try again."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not staff_confirm_invoice_paid(inv):
        return Response({"detail": "Could not mark invoice paid."}, status=status.HTTP_409_CONFLICT)
    inv.refresh_from_db()
    if inv.appointment_id:
        inv.appointment.refresh_from_db()
    return Response(
        {
            "ok": True,
            "paid": True,
            "detail": "Marked paid — payment was verified in the Square app.",
            "invoice_status": inv.status,
            "appointment_status": inv.appointment.status,
        }
    )


def _patient_document_file_path(files_base: str, doc_id: int) -> str:
    """Authenticated API path (under /api/v1) — use instead of public /media/ URLs."""
    return f"{files_base}/patient_document_file/?doc_id={doc_id}"


def _patient_document_file_on_disk(doc: "PatientDocument") -> bool:
    """True when the DB row has a file field and the bytes exist on disk (or storage backend)."""
    if not doc.file or not doc.file.name:
        return False
    try:
        return bool(doc.file.storage.exists(doc.file.name))
    except Exception:
        return False


def _serve_patient_document_file(doc: "PatientDocument", *, as_download: bool) -> FileResponse:
    """Stream an uploaded patient document (requires authenticated API access)."""
    if not doc.file:
        raise Http404("File not found.")
    try:
        file_handle = doc.file.open("rb")
    except OSError as exc:
        raise Http404("File not found.") from exc
    name = doc.original_filename or doc.file.name
    content_type, _ = mimetypes.guess_type(name)
    if not content_type:
        content_type = "application/octet-stream"
    response = FileResponse(file_handle, content_type=content_type)
    disposition = "attachment" if as_download else "inline"
    safe_name = name.replace('"', "'").replace("\n", " ").replace("\r", " ")
    response["Content-Disposition"] = f'{disposition}; filename="{safe_name}"'
    return response


def _patient_document_file_response(request) -> FileResponse | Response:
    doc_id = request.query_params.get("doc_id")
    if not doc_id:
        return Response({"detail": "doc_id is required."}, status=status.HTTP_400_BAD_REQUEST)
    doc = PatientDocument.objects.filter(pk=doc_id).first()
    if not doc:
        return Response({"detail": "Document not found."}, status=status.HTTP_404_NOT_FOUND)
    as_download = str(request.query_params.get("download", "")).lower() in ("1", "true", "yes")
    try:
        return _serve_patient_document_file(doc, as_download=as_download)
    except Http404:
        return Response(
            {
                "detail": (
                    "File not found on server. The upload may have been lost after a deploy — "
                    "please upload the document again."
                ),
            },
            status=status.HTTP_404_NOT_FOUND,
        )


def _serialize_patient_document(doc: "PatientDocument", request, *, files_base: str) -> dict:
    """Serialize a PatientDocument instance to a JSON-safe dict."""
    file_ok = _patient_document_file_on_disk(doc)
    file_path = _patient_document_file_path(files_base, doc.id) if file_ok else None
    return {
        "id": doc.id,
        "label": doc.label,
        "doc_type": doc.doc_type,
        "doc_type_display": dict(PatientDocument.DOC_TYPES).get(doc.doc_type, doc.doc_type),
        "original_filename": doc.original_filename,
        "file_available": file_ok,
        "file_path": file_path,
        "file_url": request.build_absolute_uri(f"/api/v1{file_path}") if file_path else None,
        "uploaded_by": doc.uploaded_by.get_full_name() or doc.uploaded_by.username if doc.uploaded_by else None,
        "created_at": doc.created_at.isoformat(),
    }


def _patient_document_upload_response(request, *, files_base: str) -> Response:
    """Shared upload handler for admin and doctor portals."""
    patient_id = request.data.get("patient_id")
    if not patient_id:
        return Response({"detail": "patient_id is required."}, status=status.HTTP_400_BAD_REQUEST)
    patient = Patient.objects.filter(pk=patient_id).first()
    if not patient:
        return Response({"detail": "Patient not found."}, status=status.HTTP_404_NOT_FOUND)
    file = request.FILES.get("file")
    if not file:
        return Response({"detail": "No file was uploaded."}, status=status.HTTP_400_BAD_REQUEST)
    label = (request.data.get("label") or "").strip()
    if not label:
        label = file.name
    doc_type = request.data.get("doc_type") or "other"
    valid_types = {k for k, _ in PatientDocument.DOC_TYPES}
    if doc_type not in valid_types:
        doc_type = "other"
    doc = PatientDocument.objects.create(
        patient=patient,
        uploaded_by=request.user,
        file=file,
        original_filename=file.name,
        label=label,
        doc_type=doc_type,
    )
    if not _patient_document_file_on_disk(doc):
        doc.file.delete(save=False)
        doc.delete()
        return Response(
            {
                "detail": (
                    "Upload failed: the server could not save the file. "
                    "Ask your administrator to enable persistent storage for patient documents (MEDIA volume)."
                ),
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    return Response(
        _serialize_patient_document(doc, request, files_base=files_base),
        status=status.HTTP_201_CREATED,
    )


def _serialize_patient_appointment_history(request, appointments, *, force_read_only: bool = False):
    """Build chart rows for patient_detail (visits, billing lines, handoff notes)."""
    appt_list = list(appointments)
    if not appt_list:
        return []
    patient_id = appt_list[0].patient_id
    if patient_id:
        reconcile_open_invoices_for_patient(patient_id)
    ids = [a.id for a in appt_list]
    visits = Visit.objects.filter(appointment_id__in=ids).prefetch_related(
        Prefetch(
            "rendered_services",
            queryset=VisitRenderedService.objects.select_related("service"),
        ),
        "visit_diagnoses",
    )
    visits_by_aid = {v.appointment_id: v for v in visits}
    invoices_by_aid = {
        i.appointment_id: i
        for i in Invoice.objects.filter(appointment_id__in=ids).prefetch_related(
            Prefetch(
                "payments",
                queryset=Payment.objects.filter(status=Payment.Status.SUCCESSFUL),
                to_attr="_successful_payments",
            )
        )
    }
    out = []
    for a in appt_list:
        v = visits_by_aid.get(a.id)
        inv = invoices_by_aid.get(a.id)
        lines = []
        if v:
            for rs in v.rendered_services.all():
                lines.append(
                    {
                        "service_name": rs.service.name,
                        "billing_code": rs.service.billing_code or "",
                        "quantity": rs.quantity,
                        "unit_price": str(rs.unit_price),
                        "line_total": str(rs.total_price),
                        "charges_patient": rs.charges_patient,
                    }
                )
        visit_payload = None
        if v:
            diagnoses = serialize_visit_diagnoses(v)
            visit_payload = {
                "id": v.id,
                "status": v.status,
                "reason_for_visit": v.reason_for_visit or "",
                "doctor_notes": v.doctor_notes or "",
                "diagnosis": v.diagnosis or "",
                "diagnoses": diagnoses,
                "diagnosis_ids": [d["id"] for d in diagnoses if d.get("id")],
                "completed_at": v.completed_at.isoformat() if v.completed_at else None,
                "rendered_services": lines,
            }
        inv_payload = None
        if inv:
            inv_payload = {
                "id": inv.id,
                "invoice_number": inv.invoice_number,
                "kind": inv.kind,
                "subtotal": str(inv.subtotal),
                "discount": str(inv.discount),
                "credit_applied_total": str(inv.credit_applied_total),
                "professional_discount_reason": inv.professional_discount_reason or "",
                "total_amount": str(inv.total_amount),
                "status": inv.status,
            }
            if v:
                inv_payload.update(_visit_invoice_bill_totals(v, inv))
            else:
                cached_payments = getattr(inv, "_successful_payments", None)
                if cached_payments is not None:
                    pay_sum = sum((p.amount for p in cached_payments), Decimal("0"))
                else:
                    pay_sum = inv.payments.filter(status=Payment.Status.SUCCESSFUL).aggregate(s=Sum("amount"))["s"]
                    pay_sum = pay_sum if pay_sum is not None else Decimal("0")
                payments_received = (pay_sum + inv.credit_applied_total).quantize(Decimal("0.01"))
                patient_charge = inv.total_amount.quantize(Decimal("0.01"))
                remaining_client = (patient_charge - payments_received).quantize(Decimal("0.01"))
                if remaining_client < Decimal("0"):
                    remaining_client = Decimal("0.00")
                inv_payload.update(
                    {
                        "bill_charges_total": str(inv.subtotal),
                        "patient_charge_total": str(patient_charge),
                        "insurance_remaining_total": "0.00",
                        "payments_received_total": str(payments_received),
                        "remaining_client_responsibility_total": str(remaining_client),
                    }
                )
        out.append(
            {
                "id": a.id,
                "appointment_date": str(a.appointment_date),
                "start_time": a.start_time.strftime("%I:%M %p"),
                "end_time": a.end_time.strftime("%I:%M %p"),
                "service": a.booked_service.name if a.booked_service else None,
                "booked_service_id": a.booked_service_id,
                "provider": str(a.provider) if a.provider else None,
                "provider_id": a.provider_id,
                "status": a.status,
                "clinical_handoff_notes": a.clinical_handoff_notes or "",
                "can_edit_handoff_notes": _can_edit_handoff_notes(request, a, force_read_only=force_read_only),
                "visit": visit_payload,
                "invoice": inv_payload,
            }
        )
    return out


def _save_appointment_handoff_notes(request):
    ser = AppointmentHandoffNotesSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    aid = ser.validated_data["appointment_id"]
    notes = ser.validated_data["clinical_handoff_notes"]
    appt = Appointment.objects.filter(pk=aid).select_related("provider", "patient", "booked_service").first()
    if not appt:
        return Response({"detail": "Appointment not found."}, status=status.HTTP_404_NOT_FOUND)
    prov = provider_for_doctor_user(request.user)
    if prov and clinical_access_level(prov, appt.patient) != "full":
        return Response(
            {
                "detail": "This patient is outside your care type (chiropractic vs massage). "
                "You can view their chart but cannot edit notes or demographics."
            },
            status=status.HTTP_403_FORBIDDEN,
        )
    if not _can_edit_handoff_notes(request, appt):
        return Response(
            {"detail": "You cannot edit chart notes on this appointment."},
            status=status.HTTP_403_FORBIDDEN,
        )
    appt.clinical_handoff_notes = notes
    appt.save(update_fields=["clinical_handoff_notes", "updated_at"])
    return Response({
        "detail": "Reminders and handoff saved.",
        "clinical_handoff_notes": appt.clinical_handoff_notes,
    })


_SOAP_NOTES_EDITABLE_APPOINTMENT_STATUSES = frozenset(
    {
        Appointment.Status.IN_CONSULTATION,
        Appointment.Status.AWAITING_PAYMENT,
        Appointment.Status.COMPLETED,
    }
)


def _soap_notes_edit_context(request, appointment: Appointment | None):
    """
    Validate appointment + permissions for reading or saving consultation SOAP notes.
    Returns (visit, error_response).
    """
    if not appointment:
        return None, Response({"detail": "Appointment not found."}, status=status.HTTP_404_NOT_FOUND)
    if appointment.status not in _SOAP_NOTES_EDITABLE_APPOINTMENT_STATUSES:
        return None, Response(
            {
                "detail": (
                    "SOAP notes can only be edited during consultation, while awaiting payment, "
                    "or after the visit is completed."
                ),
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    prov = provider_for_doctor_user(request.user)
    if prov and clinical_access_level(prov, appointment.patient) != "full":
        return None, Response(
            {
                "detail": "This patient is outside your care type (chiropractic vs massage). "
                "You can view their chart but cannot edit consultation notes."
            },
            status=status.HTTP_403_FORBIDDEN,
        )
    if not _can_edit_handoff_notes(request, appointment):
        return None, Response(
            {"detail": "You cannot edit consultation notes on this appointment."},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        visit = appointment.visit
    except Visit.DoesNotExist:
        return None, Response(
            {"detail": "No visit record for this appointment yet."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return visit, None


def _save_appointment_soap_notes(request):
    ser = AppointmentSoapNotesSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    aid = ser.validated_data["appointment_id"]
    notes = ser.validated_data["doctor_notes"]
    appt = Appointment.objects.filter(pk=aid).select_related("provider", "patient").first()
    visit, err = _soap_notes_edit_context(request, appt)
    if err:
        return err
    visit.doctor_notes = notes
    visit.save(update_fields=["doctor_notes", "updated_at"])
    return Response({
        "detail": "Consultation notes saved.",
        "doctor_notes": visit.doctor_notes,
    })


class BookingOptionsViewSet(viewsets.ViewSet):
    """Public endpoint: services and providers available for online booking."""

    permission_classes = [permissions.AllowAny]

    def list(self, request):
        from apps.clinic.cache_utils import CACHE_KEY_BOOKING_OPTIONS, TTL_BOOKING

        cached = cache.get(CACHE_KEY_BOOKING_OPTIONS)
        if cached is not None:
            return Response(cached)

        bookable_qs = (
            Service.objects.filter(is_active=True, show_in_public_booking=True)
            .annotate(
                _book_order=Case(
                    When(service_type=Service.ServiceType.CHIROPRACTIC, then=Value(0)),
                    When(service_type=Service.ServiceType.MASSAGE, then=Value(1)),
                    default=Value(2),
                    output_field=IntegerField(),
                )
            )
            .order_by("_book_order", "name")
        )
        bookable_list = list(
            bookable_qs.prefetch_related(
                Prefetch(
                    "providers",
                    queryset=Provider.objects.filter(active=True).select_related("user"),
                    to_attr="_active_providers",
                )
            )
        )
        services = [
            {
                "id": s.id,
                "name": s.label_for_public_booking(),
                "description": s.description or "",
                "duration_minutes": s.duration_minutes,
                "price": str(s.price),  # ensure JSON-serialisable for cache
                "service_type": s.service_type,
                "is_new_client_intake": s.is_new_client_intake,
                "allow_provider_choice": s.service_type == "massage",
            }
            for s in bookable_list
        ]
        providers_by_service = {
            svc.id: [{"id": p.id, "provider_name": str(p)} for p in svc._active_providers]
            for svc in bookable_list
        }
        apply_intake_chiropractic_provider_fallback(bookable_list, providers_by_service)
        payload = {"services": services, "providers_by_service": providers_by_service}
        cache.set(CACHE_KEY_BOOKING_OPTIONS, payload, TTL_BOOKING)
        return Response(payload)

    @action(detail=False, methods=["get"], url_path="availability")
    def availability(self, request):
        """Return available time slots for a date/provider/service. Public.

        Uses a 15-minute start grid for all service types. Chiropractic blocks ``duration_minutes``;
        massage blocks ``duration_minutes`` plus a fixed post-visit buffer on the provider calendar.
        """
        from datetime import datetime

        from .patient_phone import patient_matches_phone_normalized

        date_str = request.query_params.get("date")
        provider_id = request.query_params.get("provider_id")
        service_id = request.query_params.get("service_id")
        if not all([date_str, provider_id, service_id]):
            return Response(
                {"detail": "date, provider_id, and service_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            appt_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return Response({"detail": "Invalid date format. Use YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)
        provider = Provider.objects.filter(pk=provider_id, active=True).first()
        if not provider:
            return Response({"detail": "Invalid provider or service."}, status=status.HTTP_400_BAD_REQUEST)

        exclude_raw = (request.query_params.get("exclude_appointment_id") or "").strip()
        phone_for_exclude = (request.query_params.get("phone") or "").strip()
        reschedule_self_service = False
        ex_appt_for_reschedule = None
        if exclude_raw and phone_for_exclude:
            valid_ex, msg_ex = validate_phone(phone_for_exclude)
            if not valid_ex:
                return Response({"detail": msg_ex or "Invalid phone."}, status=status.HTTP_400_BAD_REQUEST)
            norm_ex = normalize_phone(phone_for_exclude)
            try:
                exclude_pk = int(exclude_raw)
            except ValueError:
                return Response(
                    {"detail": "exclude_appointment_id must be a number."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            ex_appt_for_reschedule = (
                Appointment.objects.select_related("patient", "booked_service")
                .filter(pk=exclude_pk, status=Appointment.Status.BOOKED)
                .first()
            )
            if (
                ex_appt_for_reschedule
                and patient_matches_phone_normalized(ex_appt_for_reschedule.patient, norm_ex)
                and ex_appt_for_reschedule.provider_id == provider.pk
            ):
                reschedule_self_service = True

        if reschedule_self_service and ex_appt_for_reschedule:
            service = ex_appt_for_reschedule.booked_service
            if not service or not service.is_active:
                return Response({"detail": "Invalid provider or service."}, status=status.HTTP_400_BAD_REQUEST)
            try:
                if int(service_id) != service.pk:
                    return Response(
                        {"detail": "That visit does not match this service."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            except (TypeError, ValueError):
                return Response({"detail": "Invalid service_id."}, status=status.HTTP_400_BAD_REQUEST)
        else:
            service = Service.objects.filter(pk=service_id, is_active=True, show_in_public_booking=True).first()
            if not service:
                return Response({"detail": "Invalid provider or service."}, status=status.HTTP_400_BAD_REQUEST)
            if not provider_can_offer_service_online(provider, service):
                return Response(
                    {"detail": "Provider does not offer this service."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        from datetime import time as time_cls

        from .online_booking_hours import (
            CHIRO_PUBLIC_BOOKING_SLOT_STEP_MINUTES,
            desk_booking_last_slot_start_minute,
            effective_desk_booking_window_minutes,
            effective_public_booking_window_minutes,
            public_booking_last_slot_start_minute,
            public_booking_treatment_duration_minutes,
        )

        desk_mode = (request.query_params.get("desk") or "").strip().lower() in ("1", "true", "yes")
        double_book_mode = (request.query_params.get("double_book") or "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        if double_book_mode:
            if not request.user.is_authenticated or getattr(request.user, "role", None) not in (
                "owner_admin",
                "staff",
            ):
                return Response(
                    {"detail": "Double-book availability requires admin or desk staff sign-in."},
                    status=status.HTTP_403_FORBIDDEN,
                )
        if desk_mode:
            if not request.user.is_authenticated or getattr(request.user, "role", None) not in (
                "owner_admin",
                "staff",
                "doctor",
            ):
                return Response(
                    {"detail": "Desk availability requires staff sign-in."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            win = effective_desk_booking_window_minutes(appt_date, service)
        else:
            win = effective_public_booking_window_minutes(appt_date, service)
        if win is None:
            return Response({"available_slots": [], "slot_start_times": [], "slot_grid": []})
        day_start, day_end = win

        required_span = public_online_booking_calendar_span_minutes(service)
        closing_compliance_span = public_booking_treatment_duration_minutes(service)
        SLOT_INTERVAL = CHIRO_PUBLIC_BOOKING_SLOT_STEP_MINUTES

        def _slot_label(h: int, m: int) -> str:
            suffix = "AM" if h < 12 else "PM"
            display_h = h if 1 <= h <= 12 else (h - 12 if h > 12 else 12)
            return f"{display_h}:{m:02d} {suffix}"

        busy_qs = (
            Appointment.objects.filter(
                provider=provider,
                appointment_date=appt_date,
            )
            .exclude(
                status__in=[
                    Appointment.Status.CANCELLED,
                    Appointment.Status.NO_SHOW,
                    Appointment.Status.COMPLETED,
                ]
            )
            .select_related("booked_service")
        )
        if reschedule_self_service and ex_appt_for_reschedule:
            busy_qs = busy_qs.exclude(pk=ex_appt_for_reschedule.pk)
        elif exclude_raw:
            if phone_for_exclude:
                valid_ex, msg_ex = validate_phone(phone_for_exclude)
                if not valid_ex:
                    return Response({"detail": msg_ex or "Invalid phone."}, status=status.HTTP_400_BAD_REQUEST)
                norm_ex = normalize_phone(phone_for_exclude)
                try:
                    exclude_pk = int(exclude_raw)
                except ValueError:
                    return Response(
                        {"detail": "exclude_appointment_id must be a number."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                ex_appt = (
                    Appointment.objects.select_related("patient")
                    .filter(pk=exclude_pk, status=Appointment.Status.BOOKED)
                    .first()
                )
                if not ex_appt:
                    return Response(
                        {"detail": "Could not verify that appointment for rescheduling."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                patient_ex = ex_appt.patient
                if not patient_matches_phone_normalized(patient_ex, norm_ex):
                    return Response(
                        {"detail": "Could not verify that appointment for rescheduling."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if ex_appt.provider_id != provider.pk:
                    return Response(
                        {"detail": "That visit is with a different provider."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if ex_appt.booked_service_id != service.id:
                    return Response(
                        {"detail": "That visit does not match this service."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                busy_qs = busy_qs.exclude(pk=exclude_pk)
            elif request.user.is_authenticated and getattr(request.user, "role", None) in (
                "doctor",
                "owner_admin",
                "staff",
            ):
                from .provider_self_schedule import user_may_manage_appointment

                try:
                    exclude_pk = int(exclude_raw)
                except ValueError:
                    return Response(
                        {"detail": "exclude_appointment_id must be a number."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                ex_appt = (
                    Appointment.objects.select_related("provider", "booked_service", "patient")
                    .filter(pk=exclude_pk)
                    .first()
                )
                if not ex_appt:
                    return Response(
                        {"detail": "Could not verify that appointment for rescheduling."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if ex_appt.status not in (
                    Appointment.Status.BOOKED,
                    Appointment.Status.CHECKED_IN,
                ):
                    return Response(
                        {"detail": "Only booked or checked-in visits can use reschedule slot preview."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if not user_may_manage_appointment(request.user, ex_appt):
                    return Response(
                        {"detail": "You do not have access to this appointment."},
                        status=status.HTTP_403_FORBIDDEN,
                    )
                if ex_appt.provider_id != provider.pk:
                    return Response(
                        {"detail": "That visit is with a different provider."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if ex_appt.booked_service_id != service.id:
                    return Response(
                        {"detail": "That visit does not match this service."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                busy_qs = busy_qs.exclude(pk=exclude_pk)
            else:
                return Response(
                    {
                        "detail": "phone is required when exclude_appointment_id is set (unless you are signed in as clinic staff)."
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        taken = set()
        for a in busy_qs.values_list("start_time", "end_time"):
            start_min = a[0].hour * 60 + a[0].minute
            end_min = a[1].hour * 60 + a[1].minute
            for m in range(start_min, end_min):
                taken.add(m)

        if desk_mode:
            last_slot_start = desk_booking_last_slot_start_minute(day_end, required_span)
        else:
            last_slot_start = public_booking_last_slot_start_minute(appt_date, day_end)

        from apps.clinic.clinic_time import slot_start_is_in_past
        from apps.clinic.timezone_utils import is_past_slot_for_clinic_today

        slot_grid: list[dict[str, object]] = []
        available: list[str] = []
        slot_start_times: list[str] = []
        cursor = day_start
        while cursor <= last_slot_start:
            h, m = divmod(cursor, 60)
            slot_start_time = time_cls(hour=h, minute=m)
            if desk_mode:
                if slot_start_is_in_past(appt_date, slot_start_time):
                    cursor += SLOT_INTERVAL
                    continue
            elif is_past_slot_for_clinic_today(slot_start_time, appt_date):
                cursor += SLOT_INTERVAL
                continue
            end_total = cursor + required_span
            end_h, end_m = divmod(end_total, 60)
            if end_h >= 24:
                end_h, end_m = 23, 59
            slot_end_time = time_cls(hour=end_h, minute=end_m)
            treat_total = cursor + closing_compliance_span
            treat_h, treat_m = divmod(treat_total, 60)
            if treat_h >= 24:
                treat_h, treat_m = 23, 59
            slot_treatment_end_time = time_cls(hour=treat_h, minute=treat_m)
            label = _slot_label(h, m)
            if desk_mode:
                fits_close = cursor + required_span <= day_end
            else:
                fits_close = cursor + closing_compliance_span <= day_end
            free = not any(cursor <= t < cursor + required_span for t in taken)
            not_blocked = (
                fits_close
                and (free or double_book_mode)
                and not provider_interval_blocked_online(
                    provider.pk,
                    appt_date,
                    slot_start_time,
                    slot_end_time,
                    block_overlap_end=slot_treatment_end_time,
                )
            )
            bookable = not_blocked
            slot_grid.append({"label": label, "bookable": bookable})
            if bookable:
                available.append(label)
                slot_start_times.append(slot_start_time.strftime("%H:%M:%S"))
            cursor += SLOT_INTERVAL

        return Response(
            {
                "available_slots": available,
                "slot_start_times": slot_start_times,
                "slot_grid": slot_grid,
                # Lets the booking UI confirm which visit length was used for this grid.
                "visit_duration_minutes": closing_compliance_span,
                "calendar_span_minutes": required_span,
                "service_id": service.id,
                "service_name": service.label_for_public_booking(),
            },
        )

    @action(detail=False, methods=["get"], url_path="patient-lookup")
    def patient_lookup(self, request):
        """Look up patient(s) by phone for booking pre-fill. Public. Same number may belong to multiple people."""
        from .chiropractic_booking_policy import (
            chiropractic_intake_context_for_new_phone_lookup,
            chiropractic_intake_context_for_patient,
        )
        from .patient_phone import names_equal_casefold, patients_matching_phone

        phone_raw = request.query_params.get("phone")
        if not phone_raw:
            return Response({"found": False})
        valid, _ = validate_phone(phone_raw)
        if not valid:
            return Response({"found": False})
        norm = normalize_phone(phone_raw)
        fn_q = (request.query_params.get("first_name") or "").strip()
        ln_q = (request.query_params.get("last_name") or "").strip()

        patients = patients_matching_phone(norm)
        if not patients:
            return Response({"found": False, **chiropractic_intake_context_for_new_phone_lookup()})

        narrowed = [p for p in patients if names_equal_casefold(p, fn_q, ln_q)] if (fn_q and ln_q) else []

        def one(patient):
            return Response(
                {
                    "found": True,
                    "ambiguous_phone": False,
                    "same_phone_different_person": False,
                    "first_name": patient.first_name,
                    "last_name": patient.last_name,
                    "email": patient.email or "",
                    "has_saved_card": bool(patient.square_card_id and patient.card_last4),
                    "card_brand": patient.card_brand or "",
                    "card_last4": patient.card_last4 or "",
                    **chiropractic_intake_context_for_patient(patient),
                }
            )

        if narrowed:
            return one(narrowed[0])

        if len(patients) == 1:
            if fn_q and ln_q:
                return Response(
                    {
                        "found": False,
                        "ambiguous_phone": False,
                        "same_phone_different_person": True,
                        **chiropractic_intake_context_for_new_phone_lookup(),
                    }
                )
            return one(patients[0])

        hm = []
        for p in patients:
            hm.append(
                {
                    "first_name": p.first_name,
                    "last_name": p.last_name,
                    "email": p.email or "",
                    "has_saved_card": bool(p.square_card_id and p.card_last4),
                    "card_brand": p.card_brand or "",
                    "card_last4": p.card_last4 or "",
                    **chiropractic_intake_context_for_patient(p),
                }
            )

        return Response(
            {
                "found": True,
                "ambiguous_phone": True,
                "same_phone_different_person": False,
                "household_members": hm,
                **chiropractic_intake_context_for_new_phone_lookup(),
            }
        )

    @action(detail=False, methods=["get"], url_path="square-config")
    def square_config(self, request):
        """Public: whether Square is enabled + Web Payments SDK ids (https://developer.squareup.com/docs/web-payments/overview)."""
        from django.conf import settings as dj_settings

        env = (getattr(dj_settings, "SQUARE_ENVIRONMENT", None) or "sandbox").strip().lower()
        return Response(
            {
                "enabled": square_configured(),
                "application_id": get_application_id() if square_configured() else "",
                "location_id": get_location_id() if square_configured() else "",
                "environment": env if square_configured() else "",
            }
        )

    @action(detail=False, methods=["post"], url_path="save-card")
    def save_card(self, request):
        """Persist a card on file using a Web Payments token (source_id from card.tokenize())."""
        if not square_configured():
            return Response({"detail": "Card registration is not enabled yet."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        from .patient_phone import get_or_create_patient_for_public_booking, patients_matching_phone

        ser = SaveSquareCardSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        phone_norm = normalize_phone(data["phone"])
        fn = (data.get("first_name") or "").strip()
        ln = (data.get("last_name") or "").strip()
        matches = patients_matching_phone(phone_norm)

        if fn and ln:
            patient = get_or_create_patient_for_public_booking(
                phone_normalized=phone_norm,
                first_name=fn,
                last_name=ln,
                email=(data.get("email") or "").strip(),
            )
        elif len(matches) == 1:
            patient = matches[0]
        elif not matches:
            return Response(
                {"detail": "Patient not found for this phone; include first_name and last_name to create the profile."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        else:
            return Response(
                {
                    "detail": (
                        "More than one person uses this phone number. Enter first and last name so we attach the "
                        "card to the right profile."
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        src = data["source_id"]
        vtok = (data.get("verification_token") or "").strip() or None
        try:
            save_card_from_source(patient, src, verification_token=vtok)
        except Exception as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(
            {
                "detail": "Card saved.",
                "card_brand": patient.card_brand,
                "card_last4": patient.card_last4,
            }
        )

    @action(detail=False, methods=["get"], url_path="my-appointments")
    def my_appointments(self, request):
        """
        List upcoming visits for this phone. Public.

        purpose=manage (default): BOOKED only, online-bookable services, future start times — reschedule/cancel.
        purpose=view: also CHECKED_IN / IN_CONSULTATION so patients can see today's visit after kiosk check-in.
        """

        from .patient_phone import patients_matching_phone

        phone_raw = request.query_params.get("phone")
        if not phone_raw:
            return Response({"detail": "phone is required."}, status=status.HTTP_400_BAD_REQUEST)
        valid, msg = validate_phone(phone_raw)
        if not valid:
            return Response({"detail": msg or "Invalid phone."}, status=status.HTTP_400_BAD_REQUEST)
        norm = normalize_phone(phone_raw)
        patients = patients_matching_phone(norm)
        if not patients:
            return Response(
                {
                    "detail": "We couldn't find a patient profile with this phone number. "
                    "Double-check the number or call the clinic for help.",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        patient_ids = [p.id for p in patients]
        shared = len(patients) > 1
        purpose = (request.query_params.get("purpose") or "manage").strip().lower()
        view_only = purpose == "view"

        now = timezone.now()
        today = now.date()
        if view_only:
            status_list = [
                Appointment.Status.BOOKED,
                Appointment.Status.CHECKED_IN,
                Appointment.Status.IN_CONSULTATION,
            ]
        else:
            status_list = [Appointment.Status.BOOKED]

        rows = (
            Appointment.objects.filter(
                patient_id__in=patient_ids,
                appointment_date__gte=today,
                status__in=status_list,
            )
            .select_related("patient", "provider", "booked_service")
            .order_by("appointment_date", "start_time")
        )
        out = []
        for a in rows:
            svc = a.booked_service
            if not view_only and a.status == Appointment.Status.BOOKED:
                if a.appointment_date == today:
                    try:
                        if _appointment_start_aware_in_clinic_tz(a) <= now:
                            continue
                    except Exception:
                        continue

            if view_only:
                if not svc:
                    service_name = "Scheduled visit"
                    service_id = 0
                    service_type = ""
                    duration_minutes = 0
                    price = "0"
                else:
                    service_name = svc.label_for_public_booking()
                    service_id = svc.id
                    service_type = svc.service_type
                    duration_minutes = svc.duration_minutes
                    price = str(svc.price)
            else:
                if not svc:
                    service_name = "Scheduled visit"
                    service_id = 0
                    service_type = ""
                    duration_minutes = 0
                    price = "0"
                else:
                    service_name = svc.label_for_public_booking()
                    service_id = svc.id
                    service_type = svc.service_type
                    duration_minutes = svc.duration_minutes
                    price = str(svc.price)

            can_cancel_online = a.status == Appointment.Status.BOOKED
            can_reschedule_online = can_cancel_online and bool(svc and svc.is_active)
            pn = a.patient
            out.append(
                {
                    "id": a.id,
                    "appointment_date": str(a.appointment_date),
                    "start_time": format_time_12h(a.start_time),
                    "service_id": service_id,
                    "service_name": service_name,
                    "service_type": service_type,
                    "provider_id": a.provider_id,
                    "provider_name": str(a.provider),
                    "duration_minutes": duration_minutes,
                    "price": price,
                    "patient_name": f"{pn.first_name} {pn.last_name}".strip(),
                    "status": a.status,
                    "can_cancel_online": can_cancel_online,
                    "can_reschedule_online": can_reschedule_online,
                    # Backward-compatible: true when cancel or reschedule is allowed online.
                    "can_manage_online": can_cancel_online or can_reschedule_online,
                }
            )
        empty_hint = ""
        if not out:
            empty_hint = self._my_appointments_empty_hint(
                patient_ids=patient_ids,
                today=today,
                now=now,
                view_only=view_only,
            )
        one = patients[0]
        return Response(
            {
                "first_name": one.first_name if not shared else "",
                "last_name": one.last_name if not shared else "",
                "email": one.email or "" if not shared else "",
                "ambiguous_phone": shared,
                "appointments": out,
                "empty_hint": empty_hint,
            }
        )

    def _my_appointments_empty_hint(
        self,
        *,
        patient_ids: list[int],
        today,
        now,
        view_only: bool,
    ) -> str:
        """Explain why my-appointments returned no rows when staff/patient expect a visit."""
        future = (
            Appointment.objects.filter(
                patient_id__in=patient_ids,
                appointment_date__gte=today,
            )
            .exclude(
                status__in=[
                    Appointment.Status.CANCELLED,
                    Appointment.Status.NO_SHOW,
                    Appointment.Status.COMPLETED,
                ]
            )
            .select_related("booked_service")
        )
        if not future.exists():
            if Appointment.objects.filter(
                patient_id__in=patient_ids,
                appointment_date__lt=today,
            ).exclude(status=Appointment.Status.CANCELLED).exists():
                return (
                    "We found older visits on this number but nothing scheduled from today onward. "
                    "If you expected a future visit, call the clinic — the number on file may differ."
                )
            return (
                "We don't see any scheduled visits on or after today for this number. "
                "Use the same cell number from when you booked, or call the clinic."
            )

        if not view_only:
            active = future.filter(
                status__in=[
                    Appointment.Status.CHECKED_IN,
                    Appointment.Status.IN_CONSULTATION,
                    Appointment.Status.AWAITING_PAYMENT,
                ]
            )
            if active.exists():
                return (
                    "You have a visit today that is already checked in or in progress. "
                    "Only visits still marked Booked (before check-in) can be changed online — call the front desk for help."
                )

        booked = future.filter(status=Appointment.Status.BOOKED)
        if booked.exists():
            hidden_service = False
            passed_today = False
            for a in booked:
                svc = a.booked_service
                if not svc or not svc.is_active or not svc.show_in_public_booking:
                    hidden_service = True
                if (
                    not view_only
                    and a.appointment_date == today
                    and svc
                    and svc.is_active
                    and svc.show_in_public_booking
                ):
                    try:
                        if _appointment_start_aware_in_clinic_tz(a) <= now:
                            passed_today = True
                    except Exception:
                        pass
            if hidden_service:
                return (
                    "A visit is on file for this number. Look it up under View / reschedule or cancel appointment."
                )
            if passed_today:
                return (
                    "Today's visit time has already started or passed for online changes. "
                    "Call the clinic if you still need to reschedule or cancel."
                )

        return (
            "No upcoming visits found for this number that can be shown online. Call the clinic if you need help."
        )

    @action(detail=False, methods=["post"], url_path="reschedule")
    def reschedule(self, request):
        """Patient self-service: move a BOOKED visit to a new open slot (phone must match). Public."""
        from .online_booking_hours import PUBLIC_BOOKING_HOURS_BLURB

        ser = PublicRescheduleSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        norm = normalize_phone(data["phone"])
        appt, err = reschedule_appointment_public(
            phone_normalized=norm,
            appointment_id=data["appointment_id"],
            new_date=data["appointment_date"],
            new_start=data["start_time"],
            sms_consent=bool(data.get("sms_consent")),
        )
        if err:
            err_lower = err.lower()
            is_slot_conflict = (
                "no longer available" in err_lower
                or "not open for online booking" in err_lower
                or err == PUBLIC_BOOKING_HOURS_BLURB
                or "pick a time later today" in err_lower
            )
            code = status.HTTP_409_CONFLICT if is_slot_conflict else status.HTTP_400_BAD_REQUEST
            return Response({"detail": err}, status=code)

        patient = appt.patient
        service = appt.booked_service
        provider = appt.provider
        return Response(
            {
                "appointment_id": appt.id,
                "status": appt.status,
                "patient": f"{patient.first_name} {patient.last_name}",
                "provider": str(provider),
                "service": service.label_for_public_booking() if service else "",
                "service_type": service.service_type if service else "",
                "appointment_date": str(appt.appointment_date),
                "start_time": format_time_12h(appt.start_time),
                "total_amount": str(service.price) if service else "0",
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="cancel-appointment")
    def cancel_appointment(self, request):
        """Patient self-service: cancel before visit start. Massage <24h: full service fee. Public."""
        ser = PublicCancelSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        norm = normalize_phone(data["phone"])
        try:
            appt, err = cancel_appointment_public(
                phone_normalized=norm,
                appointment_id=data["appointment_id"],
            )
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "cancel_appointment_public crashed appointment_id=%s",
                data.get("appointment_id"),
            )
            return Response(
                {
                    "detail": (
                        "Something went wrong cancelling online. Please try again or call the clinic "
                        "so we can cancel this visit for you."
                    ),
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if err:
            return Response({"detail": err}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "appointment_id": appt.id,
                "status": appt.status,
                "detail": "Appointment cancelled.",
            },
            status=status.HTTP_200_OK,
        )


class IsOwnerOrDoctor(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.role in {"owner_admin", "doctor", "staff"})


class IsStaffOrOwnerAdmin(permissions.BasePermission):
    """Owner and desk staff only (e.g. online booking blocks)."""

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and getattr(request.user, "role", None) in ("owner_admin", "staff")
        )


_APPT_EXCLUDED_FROM_VISIT_STATS = (
    Appointment.Status.CANCELLED,
    Appointment.Status.NO_SHOW,
)
_FUTURE_APPT_EXCLUDED = (
    Appointment.Status.CANCELLED,
    Appointment.Status.NO_SHOW,
    Appointment.Status.COMPLETED,
)


class PatientViewSet(viewsets.ModelViewSet):
    queryset = Patient.objects.all().order_by("-updated_at")
    serializer_class = PatientSerializer
    permission_classes = [IsOwnerOrDoctor]
    pagination_class = StandardPageNumberPagination

    def get_serializer_class(self):
        if self.action == "list":
            return PatientListSerializer
        return PatientSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        raw = (self.request.query_params.get("search") or "").strip()
        if raw:
            terms = [t for t in raw.split() if t]
            for term in terms:
                qs = qs.filter(
                    Q(first_name__icontains=term)
                    | Q(last_name__icontains=term)
                    | Q(email__icontains=term)
                    | Q(phone__icontains=term)
                )
        if self.action != "list":
            return qs

        qs = annotate_patient_list_stats(qs)
        directory = (self.request.query_params.get("directory") or "").strip()
        qs = apply_patient_directory_list_filter(qs, directory)
        prov = provider_for_doctor_user(self.request.user)
        if prov is not None:
            qs = filter_patient_queryset_for_provider_discipline(qs, prov)
        return qs

    def create(self, request, *args, **kwargs):
        if getattr(request.user, "role", None) not in ("owner_admin", "staff"):
            raise PermissionDenied(
                "Only clinic owner or staff can add a new patient this way. "
                "Doctors should ask the front desk or use Django admin if your clinic allows it."
            )
        return super().create(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        if getattr(request.user, "role", None) not in ("owner_admin", "staff"):
            return Response(
                {"detail": "Only clinic administrators (owner or staff) can delete a patient record."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=["post"], url_path="save-card")
    def save_card(self, request, pk=None):
        """Owner, staff, or doctor: save a payment card on file for this patient (Square Web Payments token)."""
        if not square_configured():
            return Response(
                {"detail": "Square payments are not configured. Ask your administrator to connect Square in Settings."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        patient = self.get_object()
        ser = StaffSavePatientCardSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        src = data["source_id"]
        vtok = (data.get("verification_token") or "").strip() or None
        try:
            save_card_from_source(patient, src, verification_token=vtok)
        except Exception as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(
            {
                "detail": "Card saved.",
                "card_brand": patient.card_brand,
                "card_last4": patient.card_last4,
                "has_saved_card": bool(patient.card_last4),
            }
        )


class ProviderViewSet(viewsets.ModelViewSet):
    queryset = Provider.objects.select_related("user").prefetch_related("services").all().order_by("id")
    serializer_class = ProviderSerializer
    permission_classes = [IsOwnerOrDoctor]

    def list(self, request, *args, **kwargs):
        from apps.clinic.cache_utils import CACHE_KEY_PROVIDERS, TTL_PROVIDERS

        cached = cache.get(CACHE_KEY_PROVIDERS)
        if cached is not None:
            return Response(cached)
        response = super().list(request, *args, **kwargs)
        cache.set(CACHE_KEY_PROVIDERS, response.data, TTL_PROVIDERS)
        return response

    def destroy(self, request, *args, **kwargs):
        provider = self.get_object()
        if Appointment.objects.filter(provider=provider).exists() or Visit.objects.filter(provider=provider).exists():
            return Response(
                {
                    "detail": "This provider has appointments or visit history on file. Deactivate them instead of deleting, "
                    "or reassign/remove those records first."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        user = provider.user
        with transaction.atomic():
            self.perform_destroy(provider)
            user.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"], url_path="reassign-history")
    def reassign_history(self, request, pk=None):
        """Move all appointments and visits from this provider to another (owner/staff only). Then you can remove the provider."""
        if getattr(request.user, "role", None) not in ("owner_admin", "staff"):
            return Response(
                {"detail": "Only clinic administrators can transfer provider history."},
                status=status.HTTP_403_FORBIDDEN,
            )
        src = self.get_object()
        raw_tid = request.data.get("target_provider_id")
        if raw_tid is None:
            return Response({"detail": "target_provider_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            tid = int(raw_tid)
        except (TypeError, ValueError):
            return Response({"detail": "target_provider_id must be an integer."}, status=status.HTTP_400_BAD_REQUEST)
        if tid == src.pk:
            return Response(
                {"detail": "Choose a different provider than the one you are transferring from."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        target = Provider.objects.filter(pk=tid).first()
        if not target:
            return Response({"detail": "Target provider not found."}, status=status.HTTP_404_NOT_FOUND)

        appt_count = Appointment.objects.filter(provider=src).count()
        visit_count = Visit.objects.filter(provider=src).count()
        if appt_count == 0 and visit_count == 0:
            return Response(
                {"detail": "This provider has no appointments or visits to transfer."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            Appointment.objects.filter(provider=src).update(provider=target)
            Visit.objects.filter(provider=src).update(provider=target)

        target_label = getattr(target.user, "full_name", None) or getattr(target.user, "username", None) or str(target.pk)
        return Response(
            {
                "detail": f"Transferred {appt_count} appointment(s) and {visit_count} visit(s) to {target_label}.",
                "appointments_moved": appt_count,
                "visits_moved": visit_count,
            }
        )


class ProviderUnavailabilityViewSet(viewsets.ModelViewSet):
    """Owner/staff: mark providers unavailable for public online booking (date or time window)."""

    queryset = ProviderUnavailability.objects.select_related("provider__user").all()
    serializer_class = ProviderUnavailabilitySerializer
    permission_classes = [IsStaffOrOwnerAdmin]
    http_method_names = ["get", "post", "delete", "head", "options"]

    @action(detail=False, methods=["post"], url_path="bulk")
    def bulk(self, request):
        """
        Create identical blocks for each day from ``date_from`` through ``date_to`` (inclusive).
        Use for recurring windows (e.g. same 30-minute lunch block every weekday for a month).
        """
        ser = ProviderUnavailabilityBulkSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        provider = data["provider"]
        all_day = data["all_day"]
        weekdays_only = data["weekdays_only"]
        st = data.get("start_time")
        et = data.get("end_time")
        rows: list[ProviderUnavailability] = []
        cur = data["date_from"]
        end = data["date_to"]
        while cur <= end:
            if weekdays_only and cur.weekday() >= 5:
                cur += timedelta(days=1)
                continue
            rows.append(
                ProviderUnavailability(
                    provider=provider,
                    block_date=cur,
                    all_day=all_day,
                    start_time=None if all_day else st,
                    end_time=None if all_day else et,
                ),
            )
            cur += timedelta(days=1)
        with transaction.atomic():
            ProviderUnavailability.objects.bulk_create(rows, batch_size=500)
        return Response({"created": len(rows)}, status=status.HTTP_201_CREATED)

    def get_queryset(self):
        qs = super().get_queryset().order_by("-block_date", "start_time")
        pid = self.request.query_params.get("provider_id")
        if pid:
            try:
                qs = qs.filter(provider_id=int(pid))
            except (TypeError, ValueError):
                pass
        df = self.request.query_params.get("date_from")
        dt_to = self.request.query_params.get("date_to")
        if df:
            try:
                qs = qs.filter(block_date__gte=timezone.datetime.strptime(df, "%Y-%m-%d").date())
            except ValueError:
                pass
        if dt_to:
            try:
                qs = qs.filter(block_date__lte=timezone.datetime.strptime(dt_to, "%Y-%m-%d").date())
            except ValueError:
                pass
        return qs


class ServiceViewSet(viewsets.ModelViewSet):
    queryset = Service.objects.all().order_by("name")
    serializer_class = ServiceSerializer
    permission_classes = [IsOwnerOrDoctor]

    def get_queryset(self):
        qs = super().get_queryset()
        role = getattr(self.request.user, "role", None)
        if role in ("owner_admin", "staff"):
            return qs
        if role == "doctor":
            provider = Provider.objects.filter(user=self.request.user).first()
            if not provider:
                return qs.none()
            if provider.primary_service_type == Service.ServiceType.CHIROPRACTIC:
                visibility = Q(visible_to_chiropractic_staff=True)
            elif provider.primary_service_type == Service.ServiceType.MASSAGE:
                visibility = Q(visible_to_massage_staff=True)
            else:
                visibility = Q(pk__in=[])
            date_str = (self.request.query_params.get("for_date") or "").strip()
            try:
                appt_date = timezone.datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else timezone.localdate()
            except ValueError:
                appt_date = timezone.localdate()
            booked_ids = (
                Appointment.objects.filter(
                    provider=provider,
                    appointment_date=appt_date,
                    booked_service_id__isnull=False,
                )
                .exclude(
                    status__in=[
                        Appointment.Status.CANCELLED,
                        Appointment.Status.NO_SHOW,
                        Appointment.Status.COMPLETED,
                    ]
                )
                .values_list("booked_service_id", flat=True)
                .distinct()
            )
            return qs.filter(visibility | Q(pk__in=list(booked_ids))).order_by("name").distinct()
        return qs


class DiagnosisCodeViewSet(viewsets.ModelViewSet):
    """Clinic diagnosis catalog — admin/staff maintain; doctors read active codes during consultations."""

    queryset = DiagnosisCode.objects.all().order_by("code")
    serializer_class = DiagnosisCodeSerializer
    permission_classes = [IsOwnerOrDoctor]

    def get_queryset(self):
        qs = super().get_queryset()
        role = getattr(self.request.user, "role", None)
        if role == "doctor":
            return qs.filter(is_active=True)
        return qs

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsOwnerOrDoctor()]
        return [IsStaffOrOwnerAdmin()]


class AppointmentViewSet(viewsets.ModelViewSet):
    queryset = _defer_patient_card_fields(
        Appointment.objects.select_related(
            "patient", "provider__user", "booked_service", "visit"
        ).all().order_by("appointment_date", "start_time"),
        patient_prefix="patient",
    )
    serializer_class = AppointmentSerializer
    permission_classes = [IsOwnerOrDoctor]

    def get_serializer_class(self):
        if self.action == "list":
            return AppointmentListSerializer
        return AppointmentSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        if self.action != "list":
            return qs
        qs = qs.select_related("invoice").prefetch_related("invoice__payments")
        params = self.request.query_params
        if params.get("date_from"):
            try:
                qs = qs.filter(appointment_date__gte=timezone.datetime.strptime(params["date_from"], "%Y-%m-%d").date())
            except ValueError:
                pass
        if params.get("date_to"):
            try:
                qs = qs.filter(appointment_date__lte=timezone.datetime.strptime(params["date_to"], "%Y-%m-%d").date())
            except ValueError:
                pass
        if params.get("appointment_date"):
            try:
                qs = qs.filter(appointment_date=timezone.datetime.strptime(params["appointment_date"], "%Y-%m-%d").date())
            except ValueError:
                pass
        if params.get("provider_id"):
            qs = qs.filter(provider_id=params["provider_id"])
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        return qs

    def get_permissions(self):
        if self.action in ("book", "recurring_preview", "book_recurring"):
            return [permissions.AllowAny()]
        return super().get_permissions()

    @action(detail=True, methods=["get"], url_path="prior_chart_notes")
    def prior_chart_notes(self, request, pk=None):
        """Earlier visits for this patient — handoff reminders and consultation SOAP notes."""
        from .patient_prior_chart_notes import prior_chart_notes_for_appointment

        appt = self.get_object()
        return Response({"prior_visits": prior_chart_notes_for_appointment(appt)})

    @action(detail=True, methods=["get"], url_path="book_next_context")
    def book_next_context(self, request, pk=None):
        """Completed visit summary for the Book next visit flow."""
        from .provider_self_schedule import user_may_manage_appointment

        appt = (
            Appointment.objects.select_related("patient", "provider", "booked_service")
            .filter(pk=pk)
            .first()
        )
        if not appt:
            return Response({"detail": "Appointment not found."}, status=status.HTTP_404_NOT_FOUND)
        if appt.status != Appointment.Status.COMPLETED:
            return Response(
                {"detail": "Book next is only available after a completed visit."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not user_may_manage_appointment(request.user, appt):
            return Response(
                {"detail": "You do not have access to this appointment."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return Response(book_next_context_for_appointment(appt))

    def perform_create(self, serializer):
        appt = serializer.save()
        aid = appt.id

        def queue_calendar():
            from apps.notifications.tasks import sync_appointment_google_calendar_task

            sync_appointment_google_calendar_task.delay(aid)

        def queue_doctor_alert():
            from apps.notifications.tasks import notify_provider_new_booking_task

            notify_provider_new_booking_task.delay(aid)

        transaction.on_commit(queue_calendar)
        transaction.on_commit(queue_doctor_alert)

        def queue_in_app():
            from apps.clinic.in_app_notify import create_new_booking_in_app_notification

            create_new_booking_in_app_notification(aid)

        def queue_patient_confirmations():
            from apps.clinic.patient_appointment_notifications import queue_patient_booking_confirmations

            queue_patient_booking_confirmations(aid)

        transaction.on_commit(queue_in_app)
        transaction.on_commit(queue_patient_confirmations)

    def perform_update(self, serializer):
        """If the visit time changes, allow a fresh day-before SMS reminder."""
        from datetime import datetime, timedelta

        inst = serializer.instance
        user = self.request.user
        role = getattr(user, "role", None)
        data = serializer.validated_data
        waive_late_cancel = bool(data.pop("waive_late_cancel_fee", False))
        restoring_cancelled = (
            inst.status == Appointment.Status.CANCELLED
            and data.get("status") == Appointment.Status.BOOKED
            and role in ("owner_admin", "staff")
        )
        if restoring_cancelled:
            if set(data.keys()) - {"status"}:
                raise ValidationError(
                    {
                        "detail": (
                            "When restoring a cancelled appointment, only status may be changed in this request."
                        )
                    }
                )
            _prepare_admin_uncancel_cancelled_appointment(inst)

        def _appointment_locked_as_no_show(appt: Appointment) -> bool:
            if appt.status == Appointment.Status.NO_SHOW:
                return True
            if appt.status == Appointment.Status.AWAITING_PAYMENT:
                try:
                    return appt.invoice.kind == Invoice.Kind.NO_SHOW_FEE
                except Invoice.DoesNotExist:
                    return False
            return False

        if (inst.status == Appointment.Status.CANCELLED and not restoring_cancelled) or _appointment_locked_as_no_show(
            inst
        ):
            allowed_terminal_fields = {"clinical_handoff_notes", "notes"}
            disallowed = set(data.keys()) - allowed_terminal_fields
            if disallowed:
                label = "cancelled" if inst.status == Appointment.Status.CANCELLED else "no-show"
                raise ValidationError(
                    {
                        "detail": (
                            f"This appointment is marked {label}. "
                            "Check-in, extend period, and reschedule are not available. "
                            "Book a new visit instead."
                        )
                    }
                )

        if role == "doctor":
            prov = Provider.objects.filter(user=user).first()
            if not prov or inst.provider_id != prov.id:
                raise PermissionDenied("You can only update appointments on your own schedule.")
            new_prov = data.get("provider", inst.provider)
            npid = new_prov.pk if hasattr(new_prov, "pk") else new_prov
            if npid != inst.provider_id:
                raise PermissionDenied("Only the front desk can assign this visit to another provider.")
            if "status" in data:
                new_s = data["status"]
                old_s = inst.status
                if new_s in (Appointment.Status.NO_SHOW, Appointment.Status.CANCELLED):
                    if old_s not in (
                        Appointment.Status.BOOKED,
                        Appointment.Status.CHECKED_IN,
                    ):
                        raise PermissionDenied(
                            "You can only mark no-show or cancelled before the visit is in progress or finished."
                        )
                elif new_s == Appointment.Status.COMPLETED:
                    if old_s not in (
                        Appointment.Status.IN_CONSULTATION,
                        Appointment.Status.AWAITING_PAYMENT,
                    ):
                        raise PermissionDenied(
                            "Mark completed only when the visit is in progress or awaiting payment."
                        )
            if any(k in data for k in ("appointment_date", "start_time", "end_time")):
                if inst.status in (
                    Appointment.Status.IN_CONSULTATION,
                    Appointment.Status.AWAITING_PAYMENT,
                    Appointment.Status.COMPLETED,
                ):
                    raise PermissionDenied(
                        "Ask the front desk to reschedule a visit that is already in progress, awaiting payment, or completed."
                    )

        merged_date = data.get("appointment_date", inst.appointment_date)
        merged_start = data.get("start_time", inst.start_time)
        if ("appointment_date" in data or "start_time" in data) and "end_time" not in data:
            svc = data.get("booked_service", inst.booked_service)
            if svc is not None:
                start_dt = datetime.combine(merged_date, merged_start)
                end_dt = start_dt + timedelta(minutes=svc.duration_minutes)
                data["end_time"] = end_dt.time()

        merged_end = data.get("end_time", inst.end_time)
        prov_obj = data.get("provider", inst.provider)
        overlap_pid = prov_obj.pk if hasattr(prov_obj, "pk") else inst.provider_id
        span_fields_in_request = any(
            k in data for k in ("appointment_date", "start_time", "end_time", "provider")
        )
        if span_fields_in_request:
            from .provider_self_schedule import validate_appointment_duration_span_for_desk

            svc_span = data.get("booked_service", inst.booked_service)
            prov_for_span = prov_obj if hasattr(prov_obj, "pk") else inst.provider
            err_span, _is_conflict = validate_appointment_duration_span_for_desk(
                provider=prov_for_span,
                service=svc_span,
                appt_date=merged_date,
                start_time=merged_start,
                end_time=merged_end,
                exclude_appointment_id=inst.pk,
                previous_end_time=inst.end_time if merged_end != inst.end_time else None,
            )
            if err_span:
                raise ValidationError({"detail": err_span})

        if span_fields_in_request:
            overlapping = (
                Appointment.objects.filter(
                    provider_id=overlap_pid,
                    appointment_date=merged_date,
                    start_time__lt=merged_end,
                    end_time__gt=merged_start,
                )
                .exclude(pk=inst.pk)
                .exclude(
                    status__in=[
                        Appointment.Status.CANCELLED,
                        Appointment.Status.NO_SHOW,
                        Appointment.Status.COMPLETED,
                    ]
                )
                .exists()
            )
            if overlapping:
                raise ValidationError({"detail": "That time slot is already booked for this provider."})

        if data.get("status") == Appointment.Status.CANCELLED:
            svc_cancel = inst.booked_service
            now_cancel = timezone.now()
            start_dt_cancel = timezone.make_aware(
                datetime.combine(inst.appointment_date, inst.start_time)
            )
            if (
                svc_cancel
                and svc_cancel.service_type == Service.ServiceType.MASSAGE
                and inst.status
                in (
                    Appointment.Status.BOOKED,
                    Appointment.Status.CHECKED_IN,
                )
                and not waive_late_cancel
            ):
                notice_cancel = start_dt_cancel - now_cancel
                if notice_cancel < timedelta(hours=24):
                    fee_amt = svc_cancel.price or Decimal("0")
                    if fee_amt > 0:
                        from .no_show_billing import apply_late_cancel_fee_for_appointment

                        with transaction.atomic():
                            locked = Appointment.objects.select_for_update().get(pk=inst.pk)
                            ctx = apply_late_cancel_fee_for_appointment(locked, fee_amt)
                        if ctx.get("clear_checkin"):
                            data["checked_in_at"] = None
                            data["consultation_started_at"] = None
                        if ctx.get("already_charged"):
                            inst.refresh_from_db()

        manual_no_show_card_charged = False  # used when staff marks no-show (patient billing notice)
        if data.get("status") == Appointment.Status.NO_SHOW:
            from .no_show_billing import apply_no_show_fee_for_appointment, compute_no_show_fee_for_appointment

            fee_amt = compute_no_show_fee_for_appointment(inst)
            if fee_amt > 0 and inst.status in (
                Appointment.Status.BOOKED,
                Appointment.Status.CHECKED_IN,
                Appointment.Status.AWAITING_PAYMENT,
            ):
                with transaction.atomic():
                    locked = Appointment.objects.select_for_update().get(pk=inst.pk)
                    ctx = apply_no_show_fee_for_appointment(locked, fee_amt)
                manual_no_show_card_charged = bool(ctx.get("already_charged"))
                # Keep appointment status as no_show; unpaid no-show fee is on the penalty invoice.
                if ctx.get("clear_checkin"):
                    data["checked_in_at"] = None
                    data["consultation_started_at"] = None
                if manual_no_show_card_charged:
                    inst.refresh_from_db()

        if data.get("status") == Appointment.Status.COMPLETED and inst.completed_at is None:
            data["completed_at"] = timezone.now()
        if data.get("status") == Appointment.Status.CANCELLED:
            data["completed_at"] = None
        if data.get("status") in (Appointment.Status.NO_SHOW, Appointment.Status.CANCELLED):
            if inst.status in (
                Appointment.Status.BOOKED,
                Appointment.Status.CHECKED_IN,
            ):
                data["checked_in_at"] = None
                data["consultation_started_at"] = None
        if restoring_cancelled:
            data["checked_in_at"] = None
            data["consultation_started_at"] = None
            data["completed_at"] = None

        old = {
            "appointment_date": inst.appointment_date,
            "start_time": inst.start_time,
            "end_time": inst.end_time,
            "status": inst.status,
            "provider_id": inst.provider_id,
            "booked_service_id": inst.booked_service_id,
        }
        date_changed = "appointment_date" in data and data["appointment_date"] != inst.appointment_date
        time_changed = ("start_time" in data and data["start_time"] != inst.start_time) or (
            "end_time" in data and data["end_time"] != inst.end_time
        )
        if date_changed or time_changed or restoring_cancelled:
            serializer.save(
                day_before_reminder_sms_at=None,
                day_before_reminder_email_at=None,
                same_day_reminder_sms_at=None,
                same_day_reminder_email_at=None,
            )
        else:
            serializer.save()
        new = serializer.instance
        aid = new.id

        change_lines: list[str] = []
        if old["appointment_date"] != new.appointment_date:
            change_lines.append(f"Date: {old['appointment_date']} → {new.appointment_date}.")
        if old["start_time"] != new.start_time or old["end_time"] != new.end_time:
            time_parts: list[str] = []
            if old["start_time"] != new.start_time:
                time_parts.append(
                    f"Start: {format_time_12h(old['start_time'])} → {format_time_12h(new.start_time)}"
                )
            if old["end_time"] != new.end_time:
                time_parts.append(
                    f"End: {format_time_12h(old['end_time'])} → {format_time_12h(new.end_time)}"
                )
            change_lines.append(f"{' · '.join(time_parts)}.")
        if old["status"] != new.status:
            change_lines.append(f"Status: {old['status']} → {new.status}.")
        if old["booked_service_id"] != new.booked_service_id:
            change_lines.append("Booked service changed.")
        old_provider_id = None
        old_date_iso = None
        old_time_iso = None
        if old["provider_id"] != new.provider_id:
            change_lines.append("This appointment is now on your schedule (reassigned).")
            old_provider_id = old["provider_id"]
            old_date_iso = str(old["appointment_date"])
            old_time_iso = old["start_time"].isoformat()

        def queue_calendar():
            from apps.notifications.tasks import sync_appointment_google_calendar_task

            sync_appointment_google_calendar_task.delay(aid)

        def queue_doctor_alerts():
            from apps.notifications.tasks import notify_provider_schedule_change_task

            if change_lines:
                notify_provider_schedule_change_task.delay(
                    aid,
                    change_lines,
                    old_provider_id=old_provider_id,
                    old_date_iso=old_date_iso,
                    old_time_iso=old_time_iso,
                )

        def queue_in_app():
            from apps.clinic.in_app_notify import create_schedule_change_in_app_notifications

            if change_lines:
                create_schedule_change_in_app_notifications(
                    aid,
                    change_lines,
                    old_provider_id,
                    old_date_iso,
                    old_time_iso,
                )

        transaction.on_commit(queue_calendar)
        transaction.on_commit(queue_doctor_alerts)
        transaction.on_commit(queue_in_app)

        became_cancelled = (
            old["status"] != Appointment.Status.CANCELLED
            and new.status == Appointment.Status.CANCELLED
        )
        became_uncancelled = (
            old["status"] == Appointment.Status.CANCELLED
            and new.status == Appointment.Status.BOOKED
        )
        schedule_changed = (date_changed or time_changed) and new.status in (
            Appointment.Status.BOOKED,
            Appointment.Status.CHECKED_IN,
        )
        if became_cancelled:

            def queue_patient_cancel():
                from apps.clinic.patient_appointment_notifications import queue_patient_cancel_confirmations

                queue_patient_cancel_confirmations(aid, staff_initiated=True)

            transaction.on_commit(queue_patient_cancel)
        elif became_uncancelled:

            def queue_patient_restore():
                from apps.clinic.patient_appointment_notifications import queue_patient_booking_confirmations

                queue_patient_booking_confirmations(
                    aid,
                    include_provider_notify=True,
                    include_gcal=True,
                )

            transaction.on_commit(queue_patient_restore)
        elif schedule_changed:

            def queue_patient_reschedule():
                from apps.clinic.patient_appointment_notifications import queue_patient_reschedule_confirmations

                queue_patient_reschedule_confirmations(aid, staff_initiated=True)

            transaction.on_commit(queue_patient_reschedule)

        became_no_show = (
            old["status"] != Appointment.Status.NO_SHOW
            and new.status == Appointment.Status.NO_SHOW
        )
        if became_no_show and new.auto_no_show_processed_at is None:
            charged = manual_no_show_card_charged

            def queue_no_show_billing_notice():
                from apps.clinic.models import Invoice
                from apps.clinic.no_show_patient_notice import send_no_show_patient_notice

                appt = (
                    Appointment.objects.select_related("patient", "provider", "booked_service")
                    .get(pk=aid)
                )
                inv = (
                    Invoice.objects.filter(
                        appointment_id=aid,
                        kind=Invoice.Kind.NO_SHOW_FEE,
                    )
                    .order_by("-created_at")
                    .first()
                )
                send_no_show_patient_notice(
                    appt,
                    invoice=inv,
                    card_charged=charged,
                )

            transaction.on_commit(queue_no_show_billing_notice)

    def perform_destroy(self, instance):
        from .google_calendar_sync import delete_appointment_google_event_before_db_delete

        delete_appointment_google_event_before_db_delete(instance)
        super().perform_destroy(instance)

    @action(detail=False, methods=["post"])
    def book(self, request):
        serializer = PublicBookingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data

        appointment, err = create_appointment_from_public_booking(payload)
        if err:
            code = (
                status.HTTP_409_CONFLICT
                if "slot" in err.lower() or "time is not open" in err.lower()
                else status.HTTP_400_BAD_REQUEST
            )
            return Response({"detail": err}, status=code)

        patient = appointment.patient
        service = appointment.booked_service
        provider = appointment.provider
        return Response(
            {
                "appointment_id": appointment.id,
                "status": appointment.status,
                "patient": f"{patient.first_name} {patient.last_name}",
                "provider": str(provider),
                "service": service.label_for_public_booking(),
                "service_type": service.service_type,
                "appointment_date": str(appointment.appointment_date),
                "start_time": appointment.start_time.strftime("%I:%M %p"),
                "total_amount": str(service.price),
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"], url_path="recurring-preview")
    def recurring_preview(self, request):
        """Preview recurring visit dates and whether each slot is still open."""
        from .recurring_booking import preview_recurring_slots

        serializer = RecurringBookingPreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # Always 200 so the booking UI can read ok/detail/occurrences (avoid generic “could not load” on 400).
        return Response(preview_recurring_slots(serializer.validated_data))

    @action(detail=False, methods=["post"], url_path="book-recurring")
    def book_recurring(self, request):
        """Book multiple visits on a recurring schedule (one combined confirmation)."""
        from .recurring_booking import book_recurring_from_public

        serializer = RecurringBookingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        appointments, err = book_recurring_from_public(serializer.validated_data)
        if err:
            code = (
                status.HTTP_409_CONFLICT
                if "slot" in err.lower() or "not available" in err.lower() or "time is not open" in err.lower()
                else status.HTTP_400_BAD_REQUEST
            )
            return Response({"detail": err}, status=code)

        series = appointments[0].series
        rows = []
        for appointment in appointments:
            patient = appointment.patient
            service = appointment.booked_service
            provider = appointment.provider
            rows.append(
                {
                    "appointment_id": appointment.id,
                    "status": appointment.status,
                    "patient": f"{patient.first_name} {patient.last_name}",
                    "provider": str(provider),
                    "service": service.label_for_public_booking() if service else "",
                    "service_type": service.service_type if service else "",
                    "appointment_date": str(appointment.appointment_date),
                    "start_time": appointment.start_time.strftime("%I:%M %p"),
                    "total_amount": str(service.price) if service else "0",
                }
            )
        return Response(
            {
                "series_id": series.id if series else None,
                "occurrence_count": len(rows),
                "appointments": rows,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"], url_path="recurring-preview-desk")
    def recurring_preview_desk(self, request):
        """Staff desk: preview recurring visit dates for an existing patient."""
        from .provider_self_schedule import user_may_book_as_provider
        from .recurring_booking import preview_recurring_slots_desk

        serializer = DeskRecurringBookingPreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        provider = Provider.objects.filter(pk=data["provider_id"], active=True).first()
        if not provider:
            return Response({"detail": "Invalid or inactive provider."}, status=status.HTTP_400_BAD_REQUEST)
        if not user_may_book_as_provider(request.user, provider):
            return Response({"detail": "You cannot book for that provider."}, status=status.HTTP_403_FORBIDDEN)

        return Response(preview_recurring_slots_desk(data))

    @action(detail=False, methods=["post"], url_path="book-recurring-from-desk")
    def book_recurring_from_desk(self, request):
        """Staff desk: book recurring visits for an existing patient (one combined confirmation)."""
        from .provider_self_schedule import user_may_book_as_provider
        from .recurring_booking import book_recurring_from_desk

        serializer = DeskRecurringBookingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        provider = Provider.objects.filter(pk=data["provider_id"], active=True).first()
        if not provider:
            return Response({"detail": "Invalid or inactive provider."}, status=status.HTTP_400_BAD_REQUEST)
        if not user_may_book_as_provider(request.user, provider):
            return Response({"detail": "You cannot book for that provider."}, status=status.HTTP_403_FORBIDDEN)

        appointments, err = book_recurring_from_desk(data)
        if err:
            code = (
                status.HTTP_409_CONFLICT
                if "slot" in err.lower() or "not available" in err.lower()
                else status.HTTP_400_BAD_REQUEST
            )
            return Response({"detail": err}, status=code)

        series = appointments[0].series if appointments else None
        return Response(
            {
                "detail": f"{len(appointments)} recurring visits booked.",
                "series_id": series.id if series else None,
                "occurrence_count": len(appointments),
                "appointment_ids": [a.id for a in appointments],
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="reschedule-by-provider")
    def reschedule_by_provider(self, request, pk=None):
        """Move an existing BOOKED/CHECKED_IN visit using the same slot rules as public booking."""
        import logging

        from .provider_self_schedule import (
            compute_end_time_for_slot,
            parse_appointment_date,
            parse_start_time_value,
            user_may_manage_appointment,
            validate_slot_for_desk_booking_rules,
        )

        logger = logging.getLogger(__name__)
        appt = self.get_object()
        if not user_may_manage_appointment(request.user, appt):
            return Response(
                {"detail": "You do not have access to this appointment."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if appt.status not in (Appointment.Status.BOOKED, Appointment.Status.CHECKED_IN):
            return Response(
                {"detail": "Only booked or checked-in appointments can be rescheduled this way."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not appt.booked_service:
            return Response({"detail": "This appointment has no booked service."}, status=status.HTTP_400_BAD_REQUEST)

        date_raw = (request.data.get("appointment_date") or "").strip()
        time_raw = request.data.get("start_time")
        appt_date = parse_appointment_date(date_raw)
        if not appt_date:
            return Response(
                {"detail": "Invalid or missing appointment_date (use YYYY-MM-DD)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        start_t = parse_start_time_value(time_raw if time_raw is not None else "")
        if not start_t:
            return Response({"detail": "Invalid or missing start_time."}, status=status.HTTP_400_BAD_REQUEST)

        err, is_conflict = validate_slot_for_desk_booking_rules(
            provider=appt.provider,
            service=appt.booked_service,
            appt_date=appt_date,
            start_time=start_t,
            exclude_appointment_id=appt.pk,
        )
        if err:
            code = status.HTTP_409_CONFLICT if is_conflict else status.HTTP_400_BAD_REQUEST
            return Response({"detail": err}, status=code)

        aid = appt.pk

        def queue_notifications():
            from apps.clinic.patient_appointment_notifications import queue_patient_reschedule_confirmations
            from apps.notifications.tasks import sync_appointment_google_calendar_task

            try:
                sync_appointment_google_calendar_task.delay(aid)
            except Exception:
                logger.exception("Post-commit dispatch failed (calendar) reschedule appt=%s", aid)
            queue_patient_reschedule_confirmations(aid, staff_initiated=True)

        with transaction.atomic():
            locked = (
                Appointment.objects.select_for_update(of=("self",))
                .select_related("provider", "booked_service")
                .get(pk=aid)
            )
            if locked.status not in (Appointment.Status.BOOKED, Appointment.Status.CHECKED_IN):
                return Response(
                    {"detail": "Appointment status changed; refresh and try again."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            err2, is_conflict2 = validate_slot_for_desk_booking_rules(
                provider=locked.provider,
                service=locked.booked_service,
                appt_date=appt_date,
                start_time=start_t,
                exclude_appointment_id=locked.pk,
            )
            if err2:
                code = status.HTTP_409_CONFLICT if is_conflict2 else status.HTTP_400_BAD_REQUEST
                return Response({"detail": err2}, status=code)
            locked.appointment_date = appt_date
            locked.start_time = start_t
            locked.end_time = compute_end_time_for_slot(appt_date, start_t, locked.booked_service)
            locked.clear_reminder_timestamps()
            locked.save()

        transaction.on_commit(queue_notifications)

        return Response(
            {"detail": "Appointment rescheduled.", "appointment_id": aid},
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="book-by-provider")
    def book_by_provider(self, request):
        """Book a follow-up visit after a COMPLETED appointment (new BOOKED row)."""
        import logging

        from .chiropractic_booking_policy import chiropractic_booking_must_use_intake
        from .provider_self_schedule import (
            compute_end_time_for_slot,
            parse_appointment_date,
            parse_start_time_value,
            user_may_book_as_provider,
            user_may_manage_appointment,
            validate_slot_for_desk_booking_rules,
        )

        logger = logging.getLogger(__name__)

        try:
            source_id = int(request.data.get("source_appointment_id"))
        except (TypeError, ValueError):
            return Response({"detail": "source_appointment_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        src = Appointment.objects.select_related("patient", "provider", "booked_service").filter(pk=source_id).first()
        if not src:
            return Response({"detail": "Source appointment not found."}, status=status.HTTP_400_BAD_REQUEST)
        if src.status != Appointment.Status.COMPLETED:
            return Response(
                {"detail": "Book next is only available after a completed visit."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not user_may_manage_appointment(request.user, src):
            return Response(
                {"detail": "You do not have access to this appointment."},
                status=status.HTTP_403_FORBIDDEN,
            )

        patient = src.patient

        try:
            service_id = int(request.data.get("service_id"))
            provider_id = int(request.data.get("provider_id"))
        except (TypeError, ValueError):
            return Response({"detail": "service_id and provider_id are required."}, status=status.HTTP_400_BAD_REQUEST)

        date_raw = (request.data.get("appointment_date") or "").strip()
        time_raw = request.data.get("start_time")
        appt_date = parse_appointment_date(date_raw)
        if not appt_date:
            return Response(
                {"detail": "Invalid or missing appointment_date (use YYYY-MM-DD)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        start_t = parse_start_time_value(time_raw if time_raw is not None else "")
        if not start_t:
            return Response({"detail": "Invalid or missing start_time."}, status=status.HTTP_400_BAD_REQUEST)

        service = Service.objects.filter(pk=service_id, is_active=True, show_in_public_booking=True).first()
        provider = Provider.objects.filter(pk=provider_id, active=True).first()
        if not service or not provider:
            return Response({"detail": "Invalid service or provider."}, status=status.HTTP_400_BAD_REQUEST)
        if not provider_can_offer_service_online(provider, service):
            return Response(
                {"detail": "This provider does not offer the selected service."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not user_may_book_as_provider(request.user, provider):
            return Response({"detail": "You cannot book for that provider."}, status=status.HTTP_403_FORBIDDEN)

        lapse_msg = chiropractic_booking_must_use_intake(patient, service)
        if lapse_msg:
            return Response({"detail": lapse_msg}, status=status.HTTP_400_BAD_REQUEST)

        err, is_conflict = validate_slot_for_desk_booking_rules(
            provider=provider,
            service=service,
            appt_date=appt_date,
            start_time=start_t,
            exclude_appointment_id=None,
        )
        if err:
            code = status.HTTP_409_CONFLICT if is_conflict else status.HTTP_400_BAD_REQUEST
            return Response({"detail": err}, status=code)

        end_t = compute_end_time_for_slot(appt_date, start_t, service)

        with transaction.atomic():
            err2, is_conflict2 = validate_slot_for_desk_booking_rules(
                provider=provider,
                service=service,
                appt_date=appt_date,
                start_time=start_t,
                exclude_appointment_id=None,
            )
            if err2:
                code = status.HTTP_409_CONFLICT if is_conflict2 else status.HTTP_400_BAD_REQUEST
                return Response({"detail": err2}, status=code)
            handoff_notes = (request.data.get("clinical_handoff_notes") or "").strip()
            new_appt = Appointment.objects.create(
                patient=patient,
                provider=provider,
                booked_service=service,
                appointment_date=appt_date,
                start_time=start_t,
                end_time=end_t,
                status=Appointment.Status.BOOKED,
                clinical_handoff_notes=handoff_notes,
            )

        aid = new_appt.pk

        def queue_calendar():
            from apps.notifications.tasks import sync_appointment_google_calendar_task

            try:
                sync_appointment_google_calendar_task.delay(aid)
            except Exception:
                logger.exception("Post-commit dispatch failed (calendar) book-next appt=%s", aid)

        def queue_provider_notify():
            from apps.notifications.tasks import notify_provider_new_booking_task

            try:
                notify_provider_new_booking_task.delay(aid)
            except Exception:
                logger.exception("Post-commit dispatch failed (provider notify) book-next appt=%s", aid)

        def queue_patient_msgs():
            from apps.notifications.tasks import (
                send_provider_dashboard_book_next_patient_email_task,
                send_provider_dashboard_book_next_patient_sms_task,
            )

            try:
                send_provider_dashboard_book_next_patient_sms_task.delay(aid)
            except Exception:
                logger.exception("Post-commit dispatch failed (sms) book-next appt=%s", aid)
            try:
                send_provider_dashboard_book_next_patient_email_task.delay(aid)
            except Exception:
                logger.exception("Post-commit dispatch failed (email) book-next appt=%s", aid)

        def queue_in_app():
            try:
                from apps.clinic.in_app_notify import create_new_booking_in_app_notification

                create_new_booking_in_app_notification(aid)
            except Exception:
                logger.exception("Post-commit in-app notify failed book-next appt=%s", aid)

        transaction.on_commit(queue_calendar)
        transaction.on_commit(queue_provider_notify)
        transaction.on_commit(queue_patient_msgs)
        transaction.on_commit(queue_in_app)

        return Response(
            {
                "detail": "Next visit booked.",
                "appointment_id": aid,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="book-from-desk")
    def book_from_desk(self, request):
        """Owner/staff/doctor: book an existing patient into a slot (desk hours; admin/staff may double-book)."""
        import logging

        from .chiropractic_booking_policy import chiropractic_booking_must_use_intake
        from .provider_self_schedule import (
            compute_end_time_for_slot,
            parse_appointment_date,
            parse_start_time_value,
            user_may_book_as_provider,
            validate_slot_for_desk_booking_rules,
        )

        logger = logging.getLogger(__name__)

        try:
            patient_id = int(request.data.get("patient_id"))
        except (TypeError, ValueError):
            return Response({"detail": "patient_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            service_id = int(request.data.get("service_id"))
            provider_id = int(request.data.get("provider_id"))
        except (TypeError, ValueError):
            return Response({"detail": "service_id and provider_id are required."}, status=status.HTTP_400_BAD_REQUEST)

        date_raw = (request.data.get("appointment_date") or "").strip()
        time_raw = request.data.get("start_time")
        appt_date = parse_appointment_date(date_raw)
        if not appt_date:
            return Response(
                {"detail": "Invalid or missing appointment_date (use YYYY-MM-DD)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        start_t = parse_start_time_value(time_raw if time_raw is not None else "")
        if not start_t:
            return Response({"detail": "Invalid or missing start_time."}, status=status.HTTP_400_BAD_REQUEST)

        patient = Patient.objects.filter(pk=patient_id).first()
        if not patient:
            return Response({"detail": "Patient not found."}, status=status.HTTP_400_BAD_REQUEST)

        service = Service.objects.filter(pk=service_id, is_active=True, show_in_public_booking=True).first()
        provider = Provider.objects.filter(pk=provider_id, active=True).first()
        if not service or not provider:
            return Response({"detail": "Invalid service or provider."}, status=status.HTTP_400_BAD_REQUEST)
        if not provider_can_offer_service_online(provider, service):
            return Response(
                {"detail": "This provider does not offer the selected service."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not user_may_book_as_provider(request.user, provider):
            return Response({"detail": "You cannot book for that provider."}, status=status.HTTP_403_FORBIDDEN)

        lapse_msg = chiropractic_booking_must_use_intake(patient, service)
        if lapse_msg:
            return Response({"detail": lapse_msg}, status=status.HTTP_400_BAD_REQUEST)

        allow_double_book = _truthy_request_flag(request.data.get("allow_double_book"))
        if allow_double_book and getattr(request.user, "role", None) not in ("owner_admin", "staff"):
            return Response(
                {"detail": "Only admin or desk staff may double-book a time slot."},
                status=status.HTTP_403_FORBIDDEN,
            )

        err, is_conflict = validate_slot_for_desk_booking_rules(
            provider=provider,
            service=service,
            appt_date=appt_date,
            start_time=start_t,
            exclude_appointment_id=None,
            allow_double_book=allow_double_book,
        )
        if err:
            code = status.HTTP_409_CONFLICT if is_conflict else status.HTTP_400_BAD_REQUEST
            return Response({"detail": err}, status=code)

        end_t = compute_end_time_for_slot(appt_date, start_t, service)

        with transaction.atomic():
            err2, is_conflict2 = validate_slot_for_desk_booking_rules(
                provider=provider,
                service=service,
                appt_date=appt_date,
                start_time=start_t,
                exclude_appointment_id=None,
                allow_double_book=allow_double_book,
            )
            if err2:
                code = status.HTTP_409_CONFLICT if is_conflict2 else status.HTTP_400_BAD_REQUEST
                return Response({"detail": err2}, status=code)
            new_appt = Appointment.objects.create(
                patient=patient,
                provider=provider,
                booked_service=service,
                appointment_date=appt_date,
                start_time=start_t,
                end_time=end_t,
                status=Appointment.Status.BOOKED,
            )

        aid = new_appt.pk

        def queue_calendar():
            from apps.notifications.tasks import sync_appointment_google_calendar_task

            try:
                sync_appointment_google_calendar_task.delay(aid)
            except Exception:
                logger.exception("Post-commit dispatch failed (calendar) desk-book appt=%s", aid)

        def queue_provider_notify():
            from apps.notifications.tasks import notify_provider_new_booking_task

            try:
                notify_provider_new_booking_task.delay(aid)
            except Exception:
                logger.exception("Post-commit dispatch failed (provider notify) desk-book appt=%s", aid)

        def queue_in_app():
            try:
                from apps.clinic.in_app_notify import create_new_booking_in_app_notification

                create_new_booking_in_app_notification(aid)
            except Exception:
                logger.exception("Post-commit in-app notify failed desk-book appt=%s", aid)

        def queue_patient_confirmations():
            from apps.clinic.patient_appointment_notifications import queue_patient_booking_confirmations

            queue_patient_booking_confirmations(aid)

        transaction.on_commit(queue_calendar)
        transaction.on_commit(queue_provider_notify)
        transaction.on_commit(queue_patient_confirmations)
        transaction.on_commit(queue_in_app)

        return Response(
            {"detail": "Appointment booked.", "appointment_id": aid},
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def start(self, request, pk=None):
        appointment = self.get_object()
        appointment.status = Appointment.Status.IN_CONSULTATION
        appointment.consultation_started_at = timezone.now()
        appointment.save(update_fields=["status", "consultation_started_at", "updated_at"])

        visit, _ = Visit.objects.get_or_create(
            appointment=appointment,
            defaults={
                "patient": appointment.patient,
                "provider": appointment.provider,
                "status": Visit.Status.IN_PROGRESS,
            },
        )
        if visit.status == Visit.Status.OPEN:
            visit.status = Visit.Status.IN_PROGRESS
            visit.save(update_fields=["status", "updated_at"])
        return Response({"appointment_status": appointment.status, "visit_id": visit.id})


class VisitViewSet(viewsets.ModelViewSet):
    queryset = Visit.objects.prefetch_related("rendered_services").all().order_by("-updated_at")
    serializer_class = VisitSerializer
    permission_classes = [IsOwnerOrDoctor]
    pagination_class = StandardPageNumberPagination

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        visit = self.get_object()
        serializer = VisitCompleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        invoice = complete_visit_with_services(visit, serializer.validated_data)
        return Response({"visit_status": visit.status, "invoice_id": invoice.id}, status=status.HTTP_201_CREATED)


class InvoiceViewSet(viewsets.ModelViewSet):
    queryset = _defer_patient_card_fields(
        Invoice.objects.select_related("patient", "appointment", "visit").all().order_by("-issued_at"),
        patient_prefix="patient",
    )
    serializer_class = InvoiceSerializer
    permission_classes = [IsOwnerOrDoctor]
    pagination_class = StandardPageNumberPagination

    @action(detail=True, methods=["post"])
    def pay(self, request, pk=None):
        invoice = self.get_object()
        serializer = PaymentCompleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payment = serializer.save(invoice=invoice)
        inv = serializer.result_invoice
        from apps.clinic.invoice_collection import invoice_payment_summary

        return Response(
            {
                **PaymentSerializer(payment).data,
                "invoice_id": inv.id,
                "invoice_status": inv.status,
                "fully_paid": inv.status == Invoice.Status.PAID,
                **invoice_payment_summary(inv),
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="apply_credit")
    def apply_credit(self, request, pk=None):
        """Apply available patient credit to this invoice (full or partial)."""
        invoice = self.get_object()
        if invoice.status == Invoice.Status.PAID:
            return Response({"detail": "This invoice is already paid."}, status=status.HTTP_400_BAD_REQUEST)
        if invoice.status not in (Invoice.Status.ISSUED, Invoice.Status.OVERDUE, Invoice.Status.DRAFT):
            return Response({"detail": "This invoice cannot accept credit right now."}, status=status.HTTP_400_BAD_REQUEST)

        ser = InvoiceApplyCreditSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        req_amount = ser.validated_data.get("amount")

        with transaction.atomic():
            inv = Invoice.objects.select_for_update().select_related("patient", "appointment").get(pk=invoice.pk)
            patient = Patient.objects.select_for_update().get(pk=inv.patient_id)
            due = Decimal(inv.total_amount or "0")
            available = Decimal(patient.credit_balance or "0")
            if due <= 0:
                return Response({"detail": "No remaining amount due on this invoice."}, status=status.HTTP_400_BAD_REQUEST)
            if available <= 0:
                return Response(
                    {"detail": "No patient credit available to apply.", "patient_credit_balance": str(patient.credit_balance)},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            apply_cap = req_amount if req_amount is not None else available
            if apply_cap <= 0:
                return Response({"detail": "Amount must be greater than zero."}, status=status.HTTP_400_BAD_REQUEST)
            applied = min(available, due, apply_cap)
            if applied <= 0:
                return Response({"detail": "No credit could be applied."}, status=status.HTTP_400_BAD_REQUEST)

            patient.credit_balance = (available - applied).quantize(Decimal("0.01"))
            patient.save(update_fields=["credit_balance", "updated_at"])

            PatientCreditTransaction.objects.create(
                patient=patient,
                invoice=inv,
                kind=PatientCreditTransaction.Kind.APPLY_TO_INVOICE,
                amount=applied,
                balance_after=patient.credit_balance,
                note=f"Applied to {inv.invoice_number}",
                created_by=request.user,
            )

            Payment.objects.create(
                invoice=inv,
                patient=patient,
                amount=applied,
                payment_method=Payment.Method.MANUAL,
                payment_reference=f"patient_credit:{inv.invoice_number}",
                status=Payment.Status.SUCCESSFUL,
                paid_at=timezone.now(),
            )

            inv.credit_applied_total = (Decimal(inv.credit_applied_total or "0") + applied).quantize(Decimal("0.01"))
            inv.total_amount = (due - applied).quantize(Decimal("0.01"))
            paid_now = inv.total_amount <= Decimal("0")
            if paid_now:
                inv.total_amount = Decimal("0.00")
                inv.status = Invoice.Status.PAID
                inv.paid_at = timezone.now()
                inv.save(update_fields=["credit_applied_total", "total_amount", "status", "paid_at", "updated_at"])
                _set_appointment_status_after_invoice_paid(inv)
            else:
                inv.save(update_fields=["credit_applied_total", "total_amount", "updated_at"])

        return Response(
            {
                "invoice_id": inv.id,
                "invoice_number": inv.invoice_number,
                "applied_credit": str(applied),
                "remaining_due": str(inv.total_amount),
                "patient_credit_balance": str(patient.credit_balance),
                "invoice_status": inv.status,
                "already_paid": paid_now,
            }
        )


class PaymentViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = _defer_patient_card_fields(
        Payment.objects.select_related("invoice", "patient").all().order_by("-created_at"),
        patient_prefix="patient",
    )
    serializer_class = PaymentSerializer
    permission_classes = [IsOwnerOrDoctor]
    pagination_class = StandardPageNumberPagination


class AdminViewSet(viewsets.ViewSet):
    """Admin dashboard summary. Owner/staff only."""

    permission_classes = [IsOwnerOrDoctor]

    @action(detail=False, methods=["get"], url_path="timezones")
    def timezones(self, request):
        """All valid IANA timezones grouped by region (admin Settings timezone picker)."""
        from apps.clinic.timezone_utils import get_all_timezones

        return Response(get_all_timezones())

    @action(detail=False, methods=["get"])
    def dashboard_summary(self, request):
        from apps.clinic.appointment_display import appointment_ui_status

        today = timezone.localdate()
        all_today = _defer_patient_card_fields(
            Appointment.objects.filter(appointment_date=today)
            .select_related("patient", "provider__user", "booked_service", "invoice")
            .order_by("start_time"),
            patient_prefix="patient",
        )
        appts = all_today.exclude(
            status__in=[Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW],
        )

        appointments_today = appts.count()
        checked_in = appts.filter(status=Appointment.Status.CHECKED_IN).count()
        completed = appts.filter(status=Appointment.Status.COMPLETED).count()
        no_shows_today = all_today.filter(status=Appointment.Status.NO_SHOW).count()

        from django.db.models import Sum
        daily_revenue = Invoice.objects.filter(
            status=Invoice.Status.PAID,
            paid_at__date=today,
        ).aggregate(total=Sum("total_amount"))["total"] or 0

        unpaid_invoices = Invoice.objects.filter(status=Invoice.Status.ISSUED).count()

        today_schedule = []
        for a in all_today:
            ui_status = appointment_ui_status(a)
            svc = a.booked_service
            today_schedule.append({
                "id": a.id,
                "patient_name": f"{a.patient.first_name} {a.patient.last_name}",
                "patient_payment_profile": (a.patient.payment_profile or "").strip(),
                "provider_name": str(a.provider),
                "service_name": svc.name if svc else "",
                "start_time": a.start_time.strftime("%I:%M %p").lstrip("0"),
                "end_time": a.end_time.strftime("%I:%M %p").lstrip("0"),
                "start_minutes": a.start_time.hour * 60 + a.start_time.minute,
                "status": ui_status,
                "auto_no_show": bool(a.auto_no_show_processed_at),
            })

        recent_activity = []
        for a in _defer_patient_card_fields(
            Appointment.objects.select_related("patient").filter(appointment_date=today).order_by("-updated_at")[
                :10
            ],
            patient_prefix="patient",
        ):
            if a.status == Appointment.Status.CHECKED_IN:
                recent_activity.append(
                    {
                        "text": f"{a.patient.first_name} {a.patient.last_name} completed check-in.",
                        "kind": "check_in",
                    }
                )
            elif a.status == Appointment.Status.COMPLETED:
                recent_activity.append(
                    {
                        "text": f"{a.patient.first_name} {a.patient.last_name} completed visit.",
                        "kind": "completed",
                    }
                )
            elif a.status == Appointment.Status.NO_SHOW:
                recent_activity.append(
                    {
                        "text": (
                            f"{a.patient.first_name} {a.patient.last_name} marked no-show"
                            + (" (automatic)." if a.auto_no_show_processed_at else ".")
                        ),
                        "kind": "other",
                    }
                )
        for p in _defer_patient_card_fields(
            Payment.objects.select_related("invoice__patient")
            .filter(paid_at__date=today)
            .order_by("-paid_at")[:5],
            patient_prefix="invoice__patient",
        ):
            recent_activity.append(
                {
                    "text": f"Invoice paid by {p.invoice.patient.first_name} {p.invoice.patient.last_name}.",
                    "kind": "payment",
                }
            )

        now_local = timezone.localtime(timezone.now())

        return Response({
            "appointments_today": appointments_today,
            "checked_in": checked_in,
            "completed": completed,
            "no_shows_today": no_shows_today,
            "daily_revenue": str(daily_revenue),
            "unpaid_invoices": unpaid_invoices,
            "today_schedule": today_schedule,
            "recent_activity": recent_activity[:10],
            "today_display": today.strftime("%A, %B %d, %Y"),
            "as_of_display": now_local.strftime("%I:%M %p"),
        })

    def _admin_staff_only(self, request):
        if getattr(request.user, "role", None) not in ("owner_admin", "staff"):
            return Response(
                {"detail": "Only clinic administrators (owner or staff) can view voice analytics."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return None

    @action(detail=False, methods=["get"], url_path="voice_analytics")
    def voice_analytics(self, request):
        denied = self._admin_staff_only(request)
        if denied:
            return denied
        tz = ZoneInfo(getattr(settings, "CLINIC_TIMEZONE", "America/Detroit"))
        now_local = timezone.now().astimezone(tz)
        start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        qs = VoiceCallLog.objects.filter(created_at__gte=start, created_at__lt=end)
        calls_today = qs.count()
        booked = qs.filter(outcome=VoiceCallLog.Outcome.BOOKED).count()
        # Anything that did not end with a successful voice booking (includes hang-ups on greeting).
        escalated_or_failed = qs.exclude(outcome=VoiceCallLog.Outcome.BOOKED).count()
        booked_rows = qs.filter(outcome=VoiceCallLog.Outcome.BOOKED)
        avg_sec = None
        if booked_rows.exists():
            total = 0.0
            n = 0
            for row in booked_rows:
                delta = (row.updated_at - row.created_at).total_seconds()
                if delta >= 0:
                    total += delta
                    n += 1
            if n:
                avg_sec = int(total / n)
        return Response({
            "calls_today": calls_today,
            "booked_by_voice": booked,
            "escalated_or_failed": escalated_or_failed,
            "avg_handle_seconds": avg_sec,
            "openai_configured": bool((getattr(settings, "OPENAI_API_KEY", "") or "").strip()),
        })

    @action(detail=False, methods=["get"])
    def analytics(self, request):
        """Business overview for admin analytics dashboard (owner/staff only)."""
        denied = self._admin_staff_only(request)
        if denied:
            return denied
        months = parse_analytics_months(request.query_params.get("months"))
        return Response(build_admin_analytics_payload(months=months))

    @action(detail=False, methods=["get"], url_path="voice_calls")
    def voice_calls(self, request):
        denied = self._admin_staff_only(request)
        if denied:
            return denied
        limit = min(int(request.query_params.get("limit", 50)), 100)
        qs = VoiceCallLog.objects.all().order_by("-updated_at")[:limit]
        return Response(VoiceCallLogSerializer(qs, many=True).data)

    @action(detail=False, methods=["get", "patch"], url_path="clinic_profile")
    def clinic_profile(self, request):
        """Clinic name, address, phone, hours, and bill POS code (admin Settings); persisted in DB."""
        solo = ClinicSettings.get_cached() if request.method == "GET" else ClinicSettings.get_solo()
        if request.method == "GET":
            h = _clinic_settings_bill_header()
            return Response({
                **h,
                "no_show_fee": str(solo.no_show_fee),
                "auto_no_show_enabled": solo.auto_no_show_enabled,
                "auto_no_show_grace_minutes": solo.auto_no_show_grace_minutes,
                "business_hours": list(solo.business_hours or []),
            })
        if getattr(request.user, "role", None) not in ("owner_admin", "staff"):
            return Response(
                {"detail": "Only clinic administrators (owner or staff) can update clinic settings."},
                status=status.HTTP_403_FORBIDDEN,
            )
        ser = ClinicProfileUpdateSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        if "timezone" in data:
            solo.timezone = data["timezone"]
        for field in (
            "clinic_name",
            "address_line1",
            "city_state_zip",
            "phone",
            "email",
            "employer_tax_id",
            "provider_billing_id",
            "pos_default",
        ):
            if field in data:
                val = data[field]
                setattr(
                    solo,
                    field,
                    (val or "") if field in ("email", "employer_tax_id", "provider_billing_id") else val,
                )
        if "no_show_fee" in data:
            solo.no_show_fee = data["no_show_fee"]
        if "auto_no_show_enabled" in data:
            solo.auto_no_show_enabled = data["auto_no_show_enabled"]
        if "auto_no_show_grace_minutes" in data:
            solo.auto_no_show_grace_minutes = data["auto_no_show_grace_minutes"]
        if "business_hours" in data:
            solo.business_hours = data["business_hours"]
        solo.save()
        h = _clinic_settings_bill_header()
        return Response({
            **h,
            "no_show_fee": str(solo.no_show_fee),
            "auto_no_show_enabled": solo.auto_no_show_enabled,
            "auto_no_show_grace_minutes": solo.auto_no_show_grace_minutes,
            "business_hours": list(solo.business_hours or []),
        })

    @action(detail=False, methods=["get"], url_path="payment_connection_status")
    def payment_connection_status(self, request):
        """Owner/staff: whether Square env vars look right + optional live API ping (no secrets returned)."""
        denied = self._admin_staff_only(request)
        if denied:
            return denied
        from .square_helpers import get_square_payment_status_for_admin

        return Response(get_square_payment_status_for_admin())

    @action(detail=False, methods=["post"], url_path="terminal_checkout")
    def terminal_checkout(self, request):
        """Owner/staff: send an open invoice total to the Square Terminal (visit or no-show / late-cancel fee)."""
        denied = self._admin_staff_only(request)
        if denied:
            return denied
        if not square_configured():
            return Response({"detail": "Square is not configured."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        ser = TerminalCheckoutSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        inv = Invoice.objects.filter(pk=ser.validated_data["invoice_id"]).select_related("appointment").first()
        if not inv:
            return Response({"detail": "Invoice not found."}, status=status.HTTP_404_NOT_FOUND)
        if inv.status not in (Invoice.Status.ISSUED, Invoice.Status.OVERDUE, Invoice.Status.DRAFT):
            return Response(
                {"detail": "Invoice is not awaiting payment."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        include_pending = bool(ser.validated_data.get("include_pending_fees"))
        try:
            out = create_terminal_checkout_for_invoice(inv, include_pending_fees=include_pending)
        except Exception as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(out)

    @action(detail=False, methods=["get"], url_path="visit_open_invoice")
    def visit_open_invoice(self, request):
        """Owner/staff: invoice id + total for an appointment with an unpaid visit or penalty bill."""
        denied = self._admin_staff_only(request)
        if denied:
            return denied
        aid, err = self._admin_parse_appointment_id(request, source="query")
        if err:
            return err
        appointment = Appointment.objects.filter(pk=aid).first()
        if not appointment:
            return Response({"detail": "Appointment not found."}, status=status.HTTP_404_NOT_FOUND)
        from .invoice_collection import open_invoice_for_appointment_payment

        inv = open_invoice_for_appointment_payment(appointment)
        if not inv:
            return Response({"detail": "No unpaid invoice for this appointment."}, status=status.HTTP_404_NOT_FOUND)
        from .invoice_collection import invoice_payment_summary

        return Response(
            {
                "invoice_id": inv.id,
                "invoice_number": inv.invoice_number,
                "total_amount": str(inv.total_amount),
                "kind": inv.kind,
                **invoice_payment_summary(inv),
            }
        )

    @action(detail=False, methods=["post"], url_path="terminal_checkout_test")
    def terminal_checkout_test(self, request):
        """Owner/staff: send a dollar amount to the Square Terminal to verify the device receives the prompt."""
        denied = self._admin_staff_only(request)
        if denied:
            return denied
        if not square_configured():
            return Response({"detail": "Square is not configured."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        ser = TerminalCheckoutTestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        amt = ser.validated_data["amount"]
        cents = int(Decimal(amt) * 100)
        try:
            out = create_terminal_checkout_test(cents)
        except Exception as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(out)

    @action(detail=False, methods=["post"], url_path="patient_credit_topup_terminal")
    def patient_credit_topup_terminal(self, request):
        """Owner/staff: charge card-present amount on Terminal and add it to patient wallet on completion."""
        denied = self._admin_staff_only(request)
        if denied:
            return denied
        if not square_configured():
            return Response({"detail": "Square is not configured."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        raw_pid = request.data.get("patient_id")
        raw_amount = request.data.get("amount")
        if raw_pid is None:
            return Response({"detail": "patient_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            patient_id = int(raw_pid)
        except (TypeError, ValueError):
            return Response({"detail": "Invalid patient_id."}, status=status.HTTP_400_BAD_REQUEST)
        patient = Patient.objects.filter(pk=patient_id).first()
        if not patient:
            return Response({"detail": "Patient not found."}, status=status.HTTP_404_NOT_FOUND)
        try:
            amount = Decimal(str(raw_amount))
        except Exception:
            return Response({"detail": "Invalid amount."}, status=status.HTTP_400_BAD_REQUEST)
        if amount < Decimal("1.00"):
            return Response({"detail": "Minimum top-up is $1.00."}, status=status.HTTP_400_BAD_REQUEST)
        amount = amount.quantize(Decimal("0.01"))
        try:
            out = create_terminal_checkout_for_credit_topup(
                patient_id=patient_id,
                amount_usd=amount,
                note=f"Credit top-up for {patient.first_name} {patient.last_name}",
            )
        except Exception as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(
            {
                **out,
                "patient_id": patient.id,
                "patient_name": f"{patient.first_name} {patient.last_name}",
                "amount": str(amount),
            }
        )

    @action(detail=False, methods=["post"], url_path="credit-topup-link")
    def credit_topup_link(self, request):
        """
        Public self-serve credit top-up:
        identify patient by phone (+ optional first/last for shared phones), then create Square payment link.
        """
        return Response(
            {"detail": "Self-service credit top-up is disabled. Please contact the clinic front desk."},
            status=status.HTTP_403_FORBIDDEN,
        )

        from .patient_phone import names_equal_casefold, patients_matching_phone

        if not square_configured():
            return Response({"detail": "Online credit top-up is not enabled yet."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        phone_raw = request.data.get("phone")
        amount_raw = request.data.get("amount")
        fn = (request.data.get("first_name") or "").strip()
        ln = (request.data.get("last_name") or "").strip()
        if not phone_raw:
            return Response({"detail": "phone is required."}, status=status.HTTP_400_BAD_REQUEST)
        valid, msg = validate_phone(phone_raw)
        if not valid:
            return Response({"detail": msg}, status=status.HTTP_400_BAD_REQUEST)
        try:
            amount = Decimal(str(amount_raw))
        except Exception:
            return Response({"detail": "Invalid amount."}, status=status.HTTP_400_BAD_REQUEST)
        if amount < Decimal("1.00"):
            return Response({"detail": "Minimum top-up is $1.00."}, status=status.HTTP_400_BAD_REQUEST)
        amount = amount.quantize(Decimal("0.01"))

        matches = patients_matching_phone(normalize_phone(phone_raw))
        if not matches:
            return Response(
                {"detail": "No patient profile found for that phone. Ask front desk to create your profile first."},
                status=status.HTTP_404_NOT_FOUND,
            )

        narrowed = [p for p in matches if names_equal_casefold(p, fn, ln)] if (fn and ln) else []
        if narrowed:
            patient = narrowed[0]
        elif len(matches) == 1:
            patient = matches[0]
        else:
            household = [{"first_name": p.first_name, "last_name": p.last_name} for p in matches]
            return Response(
                {
                    "detail": "More than one person uses this phone. Enter first and last name to continue.",
                    "ambiguous_phone": True,
                    "household_members": household,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        base = get_frontend_base_url()
        success_url = f"{base}/payment/success?square=1&credit_topup=1&patient={patient.id}"
        cancel_url = f"{base}/payment/cancel?square=1&credit_topup=1&patient={patient.id}"
        checkout_url, _ref = create_payment_link_for_credit_topup(
            patient_id=patient.id,
            amount_usd=amount,
            patient_label=f"{patient.first_name} {patient.last_name}",
            success_url=success_url,
            cancel_url=cancel_url,
        )
        if not checkout_url:
            return Response(
                {"detail": "Could not start online payment right now. Please try again or ask front desk."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(
            {
                "checkout_url": checkout_url,
                "patient_name": f"{patient.first_name} {patient.last_name}",
                "amount": str(amount),
                "credit_balance": str(patient.credit_balance),
            }
        )

    @action(detail=False, methods=["get"], url_path="terminal_checkout_status")
    def terminal_checkout_status(self, request):
        """Owner/staff: poll a Terminal checkout (same as doctor route; works for admin test checkouts)."""
        denied = self._admin_staff_only(request)
        if denied:
            return denied
        if not square_configured():
            return Response({"detail": "Square is not configured."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        ser = TerminalCheckoutStatusSerializer(data=request.query_params)
        ser.is_valid(raise_exception=True)
        cid = ser.validated_data["checkout_id"]
        try:
            out = get_terminal_checkout_status(cid)
        except Exception as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(out)

    @action(detail=False, methods=["get"], url_path="billing_invoices")
    def billing_invoices(self, request):
        """
        Invoices for admin billing UI. Paginated: ?page=1&page_size=25.
        Filters: list_filter, kind, search, visit_date_from, visit_date_to, insurance_only=1.
        Response includes summary counts for status tabs.
        """
        denied = self._admin_staff_only(request)
        if denied:
            return denied

        base = _defer_patient_card_fields(
            Invoice.objects.select_related("patient", "appointment", "appointment__booked_service", "visit")
            .prefetch_related(
                Prefetch(
                    "visit__rendered_services",
                    queryset=VisitRenderedService.objects.select_related("service"),
                ),
                Prefetch(
                    "payments",
                    queryset=Payment.objects.filter(status=Payment.Status.SUCCESSFUL),
                    to_attr="_successful_payments",
                ),
            ),
            patient_prefix="patient",
        )
        summary = {
            "total": base.count(),
            "open": base.filter(
                status__in=[
                    Invoice.Status.ISSUED,
                    Invoice.Status.OVERDUE,
                    Invoice.Status.DRAFT,
                ]
            ).count(),
            "overdue": base.filter(status=Invoice.Status.OVERDUE).count(),
            "paid": base.filter(status=Invoice.Status.PAID).count(),
        }

        qs = base.order_by("-issued_at", "-id")
        list_filter = (request.query_params.get("list_filter") or "all").strip().lower()
        if list_filter == "open":
            qs = qs.filter(
                status__in=[
                    Invoice.Status.ISSUED,
                    Invoice.Status.OVERDUE,
                    Invoice.Status.DRAFT,
                ]
            )
        elif list_filter == "paid":
            qs = qs.filter(status=Invoice.Status.PAID)
        elif list_filter == "overdue":
            qs = qs.filter(status=Invoice.Status.OVERDUE)

        kind = (request.query_params.get("kind") or "").strip()
        if kind:
            qs = qs.filter(kind=kind)

        search = (request.query_params.get("search") or "").strip()
        if search:
            search_q = (
                Q(patient__first_name__icontains=search)
                | Q(patient__last_name__icontains=search)
                | Q(invoice_number__icontains=search)
            )
            if search.isdigit():
                search_q |= Q(patient_id=int(search))
            qs = qs.filter(search_q)

        date_from = (request.query_params.get("visit_date_from") or "").strip()
        if date_from:
            qs = qs.filter(appointment__appointment_date__gte=date_from)
        date_to = (request.query_params.get("visit_date_to") or "").strip()
        if date_to:
            qs = qs.filter(appointment__appointment_date__lte=date_to)

        if request.query_params.get("insurance_only", "").strip().lower() in ("1", "true", "yes"):
            qs = qs.filter(visit__rendered_services__charges_patient=False).distinct()

        paginator = StandardPageNumberPagination()
        page = paginator.paginate_queryset(qs, request)
        rows = [_serialize_billing_invoice_row(inv) for inv in page]
        response = paginator.get_paginated_response(rows)
        response.data["summary"] = summary
        return response

    @action(detail=False, methods=["post"], url_path="patient_credit_topup")
    def patient_credit_topup(self, request):
        """Owner/staff: add prepaid credit to a patient's internal wallet."""
        denied = self._admin_staff_only(request)
        if denied:
            return denied
        ser = PatientCreditTopUpSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        pid = ser.validated_data["patient_id"]
        amount = Decimal(ser.validated_data["amount"])
        note = (ser.validated_data.get("note") or "").strip()

        with transaction.atomic():
            patient = Patient.objects.select_for_update().filter(pk=pid).first()
            if not patient:
                return Response({"detail": "Patient not found."}, status=status.HTTP_404_NOT_FOUND)
            patient.credit_balance = (Decimal(patient.credit_balance or "0") + amount).quantize(Decimal("0.01"))
            patient.save(update_fields=["credit_balance", "updated_at"])
            tx = PatientCreditTransaction.objects.create(
                patient=patient,
                kind=PatientCreditTransaction.Kind.TOP_UP,
                amount=amount,
                balance_after=patient.credit_balance,
                note=note,
                created_by=request.user,
            )
        return Response(
            {
                "detail": "Credit added.",
                "patient_id": patient.id,
                "patient_name": f"{patient.first_name} {patient.last_name}",
                "credited_amount": str(amount),
                "credit_balance": str(patient.credit_balance),
                "transaction_id": tx.id,
            }
        )

    @action(detail=False, methods=["get"], url_path="patient_credit_ledger")
    def patient_credit_ledger(self, request):
        denied = self._admin_staff_only(request)
        if denied:
            return denied
        raw = request.query_params.get("patient_id")
        if not raw:
            return Response({"detail": "patient_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            return Response({"detail": "Invalid patient_id."}, status=status.HTTP_400_BAD_REQUEST)
        patient = Patient.objects.filter(pk=pid).first()
        if not patient:
            return Response({"detail": "Patient not found."}, status=status.HTTP_404_NOT_FOUND)
        rows = (
            PatientCreditTransaction.objects.filter(patient_id=pid)
            .select_related("invoice", "created_by")
            .order_by("-created_at")[:100]
        )
        data = PatientCreditTransactionSerializer(rows, many=True).data
        return Response(
            {
                "patient_id": patient.id,
                "patient_name": f"{patient.first_name} {patient.last_name}",
                "credit_balance": str(patient.credit_balance),
                "transactions": data,
            }
        )

    def _admin_parse_appointment_id(self, request, *, source="query"):
        """Read appointment_id from query params (GET) or body (POST)."""
        raw = request.query_params.get("appointment_id") if source == "query" else request.data.get("appointment_id")
        if raw is None:
            return None, Response({"detail": "appointment_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            return int(raw), None
        except (TypeError, ValueError):
            return None, Response({"detail": "Invalid appointment_id."}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["get"], url_path="visit_snapshot")
    def visit_snapshot(self, request):
        """Owner/staff: chart + billing lines summary for schedule detail panel."""
        denied = self._admin_staff_only(request)
        if denied:
            return denied
        aid, err = self._admin_parse_appointment_id(request, source="query")
        if err:
            return err
        appt = (
            Appointment.objects.filter(pk=aid)
            .select_related("patient", "provider", "booked_service")
            .first()
        )
        if not appt:
            return Response({"detail": "Appointment not found."}, status=status.HTTP_404_NOT_FOUND)
        visit = (
            Visit.objects.filter(appointment=appt)
            .prefetch_related(
                Prefetch(
                    "rendered_services",
                    queryset=VisitRenderedService.objects.select_related("service").order_by("id"),
                )
            )
            .first()
        )
        inv = Invoice.objects.filter(appointment=appt).first()
        rendered = []
        if visit:
            for rs in visit.rendered_services.all():
                rendered.append({
                    "service_id": rs.service_id,
                    "service_name": rs.service.name,
                    "billing_code": rs.service.billing_code or "",
                    "quantity": rs.quantity,
                    "unit_price": str(rs.unit_price),
                    "line_total": str(rs.total_price),
                    "charges_patient": rs.charges_patient,
                })
        inv_payload = None
        if inv:
            from .invoice_collection import invoice_payment_summary

            inv_payload = {
                "id": inv.id,
                "invoice_number": inv.invoice_number,
                "kind": inv.kind,
                "subtotal": str(inv.subtotal),
                "discount": str(inv.discount),
                "credit_applied_total": str(inv.credit_applied_total),
                "professional_discount_reason": inv.professional_discount_reason or "",
                "total_amount": str(inv.total_amount),
                "status": inv.status,
                **invoice_payment_summary(inv),
            }
        return Response({
            "appointment_id": appt.id,
            "appointment_status": appt.status,
            "patient_name": f"{appt.patient.first_name} {appt.patient.last_name}",
            "patient_id": appt.patient_id,
            "provider_name": str(appt.provider),
            "provider_id": appt.provider_id,
            "booked_service_id": appt.booked_service_id,
            "service_name": appt.booked_service.name if appt.booked_service else "",
            "appointment_date": str(appt.appointment_date),
            "clinical_handoff_notes": appt.clinical_handoff_notes or "",
            "visit_id": visit.id if visit else None,
            "visit_status": visit.status if visit else None,
            "reason_for_visit": (visit.reason_for_visit or "") if visit else "",
            "doctor_notes": (visit.doctor_notes or "") if visit else "",
            "diagnosis": (visit.diagnosis or "") if visit else "",
            "rendered_services": rendered,
            "invoice": inv_payload,
        })

    def _admin_visit_billing_for_edit_context(self, appointment_id: int):
        """Load visit + invoice for admin billing editor; return (payload dict) or (None, Response)."""
        appointment = Appointment.objects.filter(pk=appointment_id).first()
        if not appointment:
            return None, Response({"detail": "Appointment not found."}, status=status.HTTP_404_NOT_FOUND)
        return _visit_billing_for_edit_payload(appointment)

    @action(detail=False, methods=["get"], url_path="visit_billing_for_edit")
    def visit_billing_for_edit(self, request):
        """Owner/staff: load visit lines for billing editor (awaiting payment or completed visits)."""
        denied = self._admin_staff_only(request)
        if denied:
            return denied
        aid, err = self._admin_parse_appointment_id(request, source="query")
        if err:
            return err
        payload, resp = self._admin_visit_billing_for_edit_context(aid)
        if resp:
            return resp
        return Response(payload)

    @action(detail=False, methods=["post"], url_path="revise_visit_billing")
    def admin_revise_visit_billing(self, request):
        """Owner/staff: update visit lines and invoice (awaiting payment or after visit completed)."""
        denied = self._admin_staff_only(request)
        if denied:
            return denied
        body = dict(request.data) if hasattr(request.data, "items") else {}
        raw_aid = body.pop("appointment_id", None)
        if raw_aid is None:
            return Response({"detail": "appointment_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            aid = int(raw_aid)
        except (TypeError, ValueError):
            return Response({"detail": "Invalid appointment_id."}, status=status.HTTP_400_BAD_REQUEST)
        visit = Visit.objects.filter(appointment_id=aid).select_related("appointment", "provider").first()
        if not visit:
            return Response({"detail": "Visit not found."}, status=status.HTTP_404_NOT_FOUND)
        ser = DoctorCompleteVisitSerializer(data=body)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        provider = visit.provider
        rendered_payload = []
        for line in data["rendered_services"]:
            svc = Service.objects.get(pk=line["service_id"])
            if not svc.is_active or not svc.visible_for_primary_service_type(provider.primary_service_type):
                return Response(
                    {
                        "detail": (
                            f'Service "{svc.name}" is not available for this provider type '
                            "or is inactive. Refresh and pick from the allowed list."
                        ),
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            unit = line["unit_price"] if line.get("unit_price") is not None else svc.price
            rendered_payload.append(
                {
                    "service_id": svc.id,
                    "quantity": line.get("quantity", 1),
                    "unit_price": str(unit),
                }
            )
        payload = _complete_visit_payload_from_validated(data, rendered_payload)
        try:
            invoice = revise_visit_billing_admin(visit, payload)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        invoice.refresh_from_db()
        followup = build_invoice_payment_followup_dict(
            invoice, try_saved_card=data.get("charge_saved_card_if_present", False)
        )
        followup.pop("already_paid", None)
        return Response(followup)

    @action(detail=False, methods=["get"], url_path="invoice_bill")
    def admin_invoice_bill(self, request):
        """Print-ready patient bill for admin. Add ?preview=1 for unpaid (issued/overdue) preview."""
        invoice_id = request.query_params.get("invoice_id")
        if not invoice_id:
            return Response({"detail": "invoice_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        inv = (
            Invoice.objects.select_related("patient", "appointment__provider", "visit")
            .prefetch_related("visit__rendered_services__service")
            .filter(pk=invoice_id)
            .first()
        )
        if not inv:
            return Response({"detail": "Invoice not found."}, status=status.HTTP_404_NOT_FOUND)
        preview = _invoice_bill_preview_requested(request)
        if preview:
            if not _invoice_bill_access_ok_for_preview(inv):
                return Response(
                    {"detail": "Cannot preview bill for this invoice status.", "invoice_status": inv.status},
                    status=status.HTTP_409_CONFLICT,
                )
            is_preview = inv.status != Invoice.Status.PAID
            return Response(_invoice_bill_dict(inv, preview=is_preview))
        if inv.status != Invoice.Status.PAID:
            return Response(
                {
                    "detail": "Patient bill is available only after the invoice is paid. Use ?preview=1 to preview before payment.",
                    "invoice_status": inv.status,
                },
                status=status.HTTP_409_CONFLICT,
            )
        return Response(_invoice_bill_dict(inv, preview=False))

    @action(detail=False, methods=["post"], url_path="email-patient-bill")
    def email_patient_bill(self, request):
        """Email paid patient bill to the patient's email (owner/staff/doctor)."""
        return _email_patient_bill_response(request)

    @action(detail=False, methods=["post"], url_path="sync-invoice-payment")
    def sync_invoice_payment(self, request):
        """Pull payment status from Square for a stuck awaiting-payment invoice."""
        return _sync_invoice_payment_api_response(request)

    @action(detail=False, methods=["post"], url_path="confirm-invoice-paid")
    def confirm_invoice_paid(self, request):
        """Mark invoice paid when staff verified payment in the Square app (sync could not match)."""
        return _confirm_invoice_paid_api_response(request)

    @action(detail=False, methods=["get"])
    def patients(self, request):
        """List all patients with directory stats and open invoice balance for admin."""
        patients_qs = annotate_patient_unpaid_balances(
            annotate_patient_list_stats(_defer_patient_card_fields(Patient.objects.all()))
        ).order_by("last_name", "first_name")
        data = []
        for p in patients_qs:
            bal_visit = getattr(p, "balance_visit", None) or Decimal("0")
            bal_no_show = getattr(p, "balance_no_show_fee", None) or Decimal("0")
            bal_late_cancel = getattr(p, "balance_late_cancel_fee", None) or Decimal("0")
            balance_total = (bal_visit + bal_no_show + bal_late_cancel).quantize(Decimal("0.01"))
            next_time = p.next_appointment_time
            data.append({
                "id": p.id,
                "first_name": p.first_name,
                "last_name": p.last_name,
                "phone": p.phone,
                "email": p.email or "",
                "no_show_count": getattr(p, "no_show_count", 0) or 0,
                "visit_count": getattr(p, "visit_count", 0) or 0,
                "last_visit": str(p.last_visit) if p.last_visit else None,
                "last_service": (getattr(p, "last_service", None) or "").strip() or None,
                "next_appointment_date": str(p.next_appointment_date)
                if getattr(p, "next_appointment_date", None)
                else None,
                "next_appointment_time": next_time.isoformat(timespec="seconds")
                if next_time
                else None,
                "date_established": str(p.effective_date_established)
                if getattr(p, "effective_date_established", None)
                else None,
                "balance": str(balance_total),
                "balance_visit": str(bal_visit.quantize(Decimal("0.01"))),
                "balance_no_show_fee": str(bal_no_show.quantize(Decimal("0.01"))),
                "balance_late_cancel_fee": str(bal_late_cancel.quantize(Decimal("0.01"))),
                "has_overdue": bool(getattr(p, "has_overdue_invoice", False)),
                "payment_profile": (p.payment_profile or "").strip(),
            })
        return Response(data)

    @action(detail=False, methods=["get"], url_path="patient_detail")
    def patient_detail(self, request):
        """Get a patient's details with full appointment history. Admin can view any patient."""
        patient_id = request.query_params.get("patient_id")
        if not patient_id:
            return Response({"detail": "patient_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            patient_id = int(patient_id)
        except (ValueError, TypeError):
            return Response({"detail": "Invalid patient_id."}, status=status.HTTP_400_BAD_REQUEST)
        patient = Patient.objects.filter(pk=patient_id).first()
        if not patient:
            return Response({"detail": "Patient not found."}, status=status.HTTP_404_NOT_FOUND)
        appointments = (
            Appointment.objects.filter(patient=patient)
            .select_related("booked_service", "provider__user")
            .order_by("-appointment_date", "-start_time")[:100]
        )
        return Response(
            {
                "id": patient.id,
                "first_name": patient.first_name,
                "last_name": patient.last_name,
                "phone": patient.phone,
                "email": patient.email or "",
                "date_of_birth": str(patient.date_of_birth) if patient.date_of_birth else None,
                "address_line1": patient.address_line1 or "",
                "address_line2": patient.address_line2 or "",
                "city_state_zip": patient.city_state_zip or "",
                "emergency_contact_name": patient.emergency_contact_name or "",
                "emergency_contact_phone": patient.emergency_contact_phone or "",
                "card_brand": patient.card_brand or "",
                "card_last4": patient.card_last4 or "",
                "has_saved_card": bool(patient.card_last4),
                "online_chiro_intake_waived": patient.online_chiro_intake_waived,
                "payment_profile": (patient.payment_profile or "").strip(),
                "sms_consent": patient.sms_consent,
                "sms_consent_at": patient.sms_consent_at.isoformat() if patient.sms_consent_at else None,
                **patient_communication_prefs_payload(patient),
                "appointments": _serialize_patient_appointment_history(request, appointments),
                **patient_demographics_summary(patient),
                "account_summary": patient_account_summary(patient),
            }
        )

    @action(detail=False, methods=["patch"], url_path="patient_intake")
    def patient_intake(self, request):
        """Update any patient profile / intake field (owner/staff). Doctors: demographics only."""
        patient_id = request.data.get("patient_id")
        if not patient_id:
            return Response({"detail": "patient_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            patient_id = int(patient_id)
        except (ValueError, TypeError):
            return Response({"detail": "Invalid patient_id."}, status=status.HTTP_400_BAD_REQUEST)
        patient = Patient.objects.filter(pk=patient_id).first()
        if not patient:
            return Response({"detail": "Patient not found."}, status=status.HTTP_404_NOT_FOUND)
        ser = PatientIntakeUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = dict(ser.validated_data)
        role = getattr(request.user, "role", None)
        allow_identity = role in ("owner_admin", "staff")
        if not allow_identity:
            for key in ("first_name", "last_name", "phone", "email"):
                data.pop(key, None)
        if role not in ("owner_admin", "staff"):
            data.pop("date_established", None)
            data.pop("online_chiro_intake_waived", None)
            data.pop("sms_consent", None)
        err = apply_patient_intake_validated_data(
            patient,
            data,
            allow_identity_fields=allow_identity,
            allow_date_established=role in ("owner_admin", "staff"),
            allow_online_waived=role in ("owner_admin", "staff"),
            allow_sms_consent=role in ("owner_admin", "staff"),
            allow_communication_prefs=True,
        )
        if err:
            return Response({"detail": err}, status=status.HTTP_400_BAD_REQUEST)
        patient.save()
        return Response({"detail": "Saved."})

    @action(detail=False, methods=["patch"], url_path="appointment_handoff")
    def appointment_handoff(self, request):
        """Save per-appointment chart / handoff notes (owner/staff may edit any appointment)."""
        if getattr(request.user, "role", None) not in ("owner_admin", "staff"):
            return Response(
                {"detail": "Only clinic administrators can use this endpoint."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return _save_appointment_handoff_notes(request)

    @action(detail=False, methods=["get"], url_path="patient_documents")
    def patient_documents(self, request):
        """List all documents attached to a patient's record."""
        patient_id = request.query_params.get("patient_id")
        if not patient_id:
            return Response({"detail": "patient_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        patient = Patient.objects.filter(pk=patient_id).first()
        if not patient:
            return Response({"detail": "Patient not found."}, status=status.HTTP_404_NOT_FOUND)
        docs = PatientDocument.objects.filter(patient=patient).select_related("uploaded_by")
        return Response([_serialize_patient_document(d, request, files_base="/admin") for d in docs])

    @action(detail=False, methods=["get"], url_path="patient_document_file")
    def patient_document_file(self, request):
        """Download or inline-preview a patient document (authenticated)."""
        return _patient_document_file_response(request)

    @action(
        detail=False,
        methods=["post"],
        url_path="patient_document_upload",
        parser_classes=[MultiPartParser, FormParser],
    )
    def patient_document_upload(self, request):
        """Upload a new document to a patient's record (multipart/form-data)."""
        return _patient_document_upload_response(request, files_base="/admin")

    @action(detail=False, methods=["delete"], url_path="patient_document_delete")
    def patient_document_delete(self, request):
        """Delete a patient document by its id."""
        doc_id = request.query_params.get("doc_id")
        if not doc_id:
            return Response({"detail": "doc_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        doc = PatientDocument.objects.filter(pk=doc_id).first()
        if not doc:
            return Response({"detail": "Document not found."}, status=status.HTTP_404_NOT_FOUND)
        doc.file.delete(save=False)
        doc.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class IsDoctor(permissions.BasePermission):
    """Only allow users with role=doctor."""

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.role == "doctor")


class DoctorViewSet(viewsets.ViewSet):
    """Doctor-only endpoints. All data filtered by the logged-in doctor's provider."""

    permission_classes = [IsDoctor]

    def _get_provider(self, request):
        provider = Provider.objects.filter(user=request.user).first()
        if not provider:
            return None
        return provider

    @action(detail=False, methods=["get"])
    def me(self, request):
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked to this account."}, status=status.HTTP_403_FORBIDDEN)
        return Response(
            {
                "provider_id": provider.id,
                "provider_name": str(provider),
                "full_name": request.user.full_name or request.user.username,
            }
        )

    @action(detail=False, methods=["get"], url_path="my-analytics")
    def my_analytics(self, request):
        """Performance and patient stats for the logged-in doctor's provider."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        weeks = parse_analytics_weeks(request.query_params.get("weeks"))
        return Response(build_doctor_my_analytics_payload(provider, weeks=weeks))

    @action(detail=False, methods=["get"])
    def appointments(self, request):
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        appt_list = appointments_for_doctor_dashboard(provider, request)
        return Response(serialize_doctor_dashboard_appointments(appt_list))

    @action(detail=False, methods=["get"])
    def patients(self, request):
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        patient_ids = list(
            Appointment.objects.filter(provider=provider).values_list("patient_id", flat=True).distinct()
        )
        if not patient_ids:
            return Response([])
        patients_qs = _defer_patient_card_fields(
            Patient.objects.filter(id__in=patient_ids).order_by("last_name", "first_name")
        )
        today = timezone.localdate()
        ex_future = [Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW, Appointment.Status.COMPLETED]

        future_ordered = list(
            Appointment.objects.filter(
                provider=provider, patient_id__in=patient_ids, appointment_date__gte=today
            )
            .exclude(status__in=ex_future)
            .order_by("appointment_date", "start_time")
        )
        next_by_pid: dict = {}
        for a in future_ordered:
            next_by_pid.setdefault(a.patient_id, a)

        visits_done = list(
            Visit.objects.filter(
                provider=provider,
                patient_id__in=patient_ids,
                status=Visit.Status.COMPLETED,
                completed_at__isnull=False,
                completed_at__lte=timezone.now(),
            ).order_by("-completed_at")
        )
        last_completed_by_pid: dict = {}
        for v in visits_done:
            last_completed_by_pid.setdefault(v.patient_id, v)

        open_inv_patient_ids = set(
            Invoice.objects.filter(
                patient_id__in=patient_ids,
                visit__provider=provider,
                status__in=[Invoice.Status.ISSUED, Invoice.Status.OVERDUE],
            ).values_list("patient_id", flat=True)
        )

        seen_since = timezone.now() - timezone.timedelta(days=30)
        seen_30_patient_ids = set(
            Visit.objects.filter(
                provider=provider,
                patient_id__in=patient_ids,
                status=Visit.Status.COMPLETED,
                completed_at__gte=seen_since,
            ).values_list("patient_id", flat=True)
        )

        data = []
        for p in patients_qs:
            na = next_by_pid.get(p.id)
            lv = last_completed_by_pid.get(p.id)
            if lv and lv.completed_at:
                last_visit_iso = timezone.localtime(lv.completed_at).date().isoformat()
            else:
                last_visit_iso = None

            next_appt_str = None
            next_status = None
            if na:
                next_appt_str = f"{na.appointment_date} {na.start_time.strftime('%I:%M %p')}"
                next_status = na.status

            data.append(
                {
                    "id": p.id,
                    "name": f"{p.first_name} {p.last_name}".strip() or "Patient",
                    "phone": p.phone or "",
                    "email": (p.email or "").strip(),
                    "last_visit": last_visit_iso,
                    "next_appt": next_appt_str,
                    "next_appointment_status": next_status,
                    "has_upcoming": bool(na),
                    "has_open_invoice": p.id in open_inv_patient_ids,
                    "seen_last_30_days": p.id in seen_30_patient_ids,
                }
            )
        return Response(data)

    @action(detail=False, methods=["get"], url_path="patient_detail")
    def patient_detail(self, request):
        """Full chart and visit history for any clinic patient (matches doctor directory search)."""
        if not self._get_provider(request):
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        patient_id = request.query_params.get("patient_id")
        if not patient_id:
            return Response({"detail": "patient_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            patient_id = int(patient_id)
        except (ValueError, TypeError):
            return Response({"detail": "Invalid patient_id."}, status=status.HTTP_400_BAD_REQUEST)
        patient = Patient.objects.filter(pk=patient_id).select_related().first()
        if not patient:
            return Response({"detail": "Patient not found."}, status=status.HTTP_404_NOT_FOUND)
        provider = self._get_provider(request)
        access = clinical_access_level(provider, patient)
        appointments = (
            Appointment.objects.filter(patient=patient)
            .select_related("booked_service", "provider__user", "patient")
            .order_by("-appointment_date", "-start_time")[:100]
        )
        return Response(
            {
                "id": patient.id,
                "first_name": patient.first_name,
                "last_name": patient.last_name,
                "phone": patient.phone,
                "email": patient.email or "",
                "date_of_birth": str(patient.date_of_birth) if patient.date_of_birth else None,
                "address_line1": patient.address_line1 or "",
                "address_line2": patient.address_line2 or "",
                "city_state_zip": patient.city_state_zip or "",
                "emergency_contact_name": patient.emergency_contact_name or "",
                "emergency_contact_phone": patient.emergency_contact_phone or "",
                "card_brand": patient.card_brand or "",
                "card_last4": patient.card_last4 or "",
                "has_saved_card": bool(patient.card_last4),
                "online_chiro_intake_waived": patient.online_chiro_intake_waived,
                "payment_profile": (patient.payment_profile or "").strip(),
                "sms_consent": patient.sms_consent,
                "sms_consent_at": patient.sms_consent_at.isoformat() if patient.sms_consent_at else None,
                **patient_communication_prefs_payload(patient),
                "clinical_access": access,
                "clinical_access_message": clinical_access_message(provider, access),
                "appointments": _serialize_patient_appointment_history(
                    request, appointments, force_read_only=(access == "read_only")
                ),
                **patient_demographics_summary(patient),
                "account_summary": patient_account_summary(patient),
            }
        )

    @action(detail=False, methods=["patch"], url_path="patient_intake")
    def patient_intake(self, request):
        """Update intake / address fields for any clinic patient."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        patient_id = request.data.get("patient_id")
        if not patient_id:
            return Response({"detail": "patient_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            patient_id = int(patient_id)
        except (ValueError, TypeError):
            return Response({"detail": "Invalid patient_id."}, status=status.HTTP_400_BAD_REQUEST)
        patient = Patient.objects.filter(pk=patient_id).first()
        if not patient:
            return Response({"detail": "Patient not found."}, status=status.HTTP_404_NOT_FOUND)
        if clinical_access_level(provider, patient) != "full":
            return Response(
                {
                    "detail": "This patient is outside your care type (chiropractic vs massage). "
                    "Ask the front desk or the other provider to update demographics."
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        ser = PatientIntakeUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = dict(ser.validated_data)
        data.pop("online_chiro_intake_waived", None)
        data.pop("date_established", None)
        err = apply_patient_intake_validated_data(
            patient,
            data,
            allow_identity_fields=True,
            allow_communication_prefs=True,
            allow_sms_consent=True,
        )
        if err:
            return Response({"detail": err}, status=status.HTTP_400_BAD_REQUEST)
        patient.save()
        return Response({"detail": "Saved."})

    @action(detail=False, methods=["patch"], url_path="appointment_handoff")
    def appointment_handoff(self, request):
        """Save chart / handoff notes on appointments assigned to this doctor."""
        return _save_appointment_handoff_notes(request)

    @action(detail=False, methods=["get"], url_path="patient_documents")
    def patient_documents(self, request):
        """List all documents attached to a patient's record."""
        patient_id = request.query_params.get("patient_id")
        if not patient_id:
            return Response({"detail": "patient_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        patient = Patient.objects.filter(pk=patient_id).first()
        if not patient:
            return Response({"detail": "Patient not found."}, status=status.HTTP_404_NOT_FOUND)
        docs = PatientDocument.objects.filter(patient=patient).select_related("uploaded_by")
        return Response([_serialize_patient_document(d, request, files_base="/doctor") for d in docs])

    @action(detail=False, methods=["get"], url_path="patient_document_file")
    def patient_document_file(self, request):
        """Download or inline-preview a patient document (authenticated)."""
        return _patient_document_file_response(request)

    @action(
        detail=False,
        methods=["post"],
        url_path="patient_document_upload",
        parser_classes=[MultiPartParser, FormParser],
    )
    def patient_document_upload(self, request):
        """Upload a new document to a patient's record (multipart/form-data)."""
        return _patient_document_upload_response(request, files_base="/doctor")

    @action(detail=False, methods=["delete"], url_path="patient_document_delete")
    def patient_document_delete(self, request):
        """Delete a patient document by its id."""
        doc_id = request.query_params.get("doc_id")
        if not doc_id:
            return Response({"detail": "doc_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        doc = PatientDocument.objects.filter(pk=doc_id).first()
        if not doc:
            return Response({"detail": "Document not found."}, status=status.HTTP_404_NOT_FOUND)
        doc.file.delete(save=False)
        doc.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=["get"], url_path="invoice_payment_status")
    def invoice_payment_status(self, request):
        """Whether an invoice is paid (for print gating after Checkout / Terminal / webhook delay)."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        invoice_id = request.query_params.get("invoice_id")
        if not invoice_id:
            return Response({"detail": "invoice_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        inv = Invoice.objects.filter(pk=invoice_id).select_related("appointment").first()
        if not inv:
            return Response({"detail": "Invoice not found."}, status=status.HTTP_404_NOT_FOUND)
        include_pending = str(request.query_params.get("include_pending_fees", "")).lower() in (
            "1",
            "true",
            "yes",
        )
        if include_pending:
            from .patient_payment_pending import invoice_ids_for_doctor_bundle
            from .square_payment import try_reconcile_bundle_terminal_payment, try_reconcile_invoice_from_square

            bundle_ids = list(invoice_ids_for_doctor_bundle(inv, include_pending_fees=True))
            try_reconcile_bundle_terminal_payment(inv)
            for bid in bundle_ids:
                sibling = Invoice.objects.filter(pk=bid).first()
                if sibling and sibling.status != Invoice.Status.PAID:
                    try_reconcile_invoice_from_square(sibling)
            inv.refresh_from_db()
            statuses = list(Invoice.objects.filter(pk__in=bundle_ids).values_list("status", flat=True))
            all_paid = bool(statuses) and all(s == Invoice.Status.PAID for s in statuses)
            return Response(
                {
                    "paid": all_paid,
                    "status": inv.status,
                    "bundle_invoice_ids": bundle_ids,
                }
            )
        if inv.status != Invoice.Status.PAID:
            try_reconcile_invoice_from_square(inv)
            inv.refresh_from_db()
        paid = inv.status == Invoice.Status.PAID
        return Response({"paid": paid, "status": inv.status})

    @action(detail=False, methods=["get"], url_path="patient_pending_payment")
    def patient_pending_payment(self, request):
        """Open penalty balances for a patient (consultation + payment UI)."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        raw_pid = request.query_params.get("patient_id")
        if not raw_pid:
            return Response({"detail": "patient_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            patient_id = int(raw_pid)
        except (TypeError, ValueError):
            return Response({"detail": "Invalid patient_id."}, status=status.HTTP_400_BAD_REQUEST)
        current_invoice_id = request.query_params.get("current_invoice_id")
        cur_id: int | None = None
        if current_invoice_id:
            try:
                cur_id = int(current_invoice_id)
            except (TypeError, ValueError):
                return Response({"detail": "Invalid current_invoice_id."}, status=status.HTTP_400_BAD_REQUEST)
        from .patient_payment_pending import build_doctor_pending_payment_context

        return Response(
            build_doctor_pending_payment_context(patient_id, current_invoice_id=cur_id),
        )

    @action(detail=False, methods=["post"], url_path="sync-invoice-payment")
    def sync_invoice_payment(self, request):
        """Pull payment status from Square for a stuck awaiting-payment invoice."""
        if not self._get_provider(request):
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        return _sync_invoice_payment_api_response(request)

    @action(detail=False, methods=["get"], url_path="invoice_bill")
    def invoice_bill(self, request):
        """Print-ready patient bill for any clinic invoice. Add ?preview=1 before payment (issued/overdue)."""
        if not self._get_provider(request):
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        invoice_id = request.query_params.get("invoice_id")
        if not invoice_id:
            return Response({"detail": "invoice_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        inv = (
            Invoice.objects.select_related("patient", "appointment__provider", "visit")
            .prefetch_related("visit__rendered_services__service")
            .filter(pk=invoice_id)
            .first()
        )
        if not inv:
            return Response({"detail": "Invoice not found."}, status=status.HTTP_404_NOT_FOUND)
        preview = _invoice_bill_preview_requested(request)
        if preview:
            if not _invoice_bill_access_ok_for_preview(inv):
                return Response(
                    {"detail": "Cannot preview bill for this invoice status.", "invoice_status": inv.status},
                    status=status.HTTP_409_CONFLICT,
                )
            is_preview = inv.status != Invoice.Status.PAID
            return Response(_invoice_bill_dict(inv, preview=is_preview))
        if inv.status != Invoice.Status.PAID:
            return Response(
                {
                    "detail": "Patient bill printing is available only after the invoice is paid. "
                    "Use ?preview=1 to preview before payment, or finish card payment / desk checkout first.",
                    "invoice_status": inv.status,
                },
                status=status.HTTP_409_CONFLICT,
            )
        return Response(_invoice_bill_dict(inv, preview=False))

    @action(detail=False, methods=["post"], url_path="email-patient-bill")
    def email_patient_bill(self, request):
        """Email paid patient bill to the patient's email on file."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        return _email_patient_bill_response(request, provider=provider)

    @action(detail=False, methods=["get"], url_path="invoice_search")
    def invoice_search(self, request):
        """Search invoices by patient name, invoice number, or date for bill reprinting."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        q = (request.query_params.get("q") or "").strip()
        if not q:
            return Response([])
        qs = Invoice.objects.filter(
            appointment__provider=provider
        ).select_related("patient", "appointment").order_by("-appointment__appointment_date")
        from django.db.models import Q
        filters = Q(patient__first_name__icontains=q) | Q(patient__last_name__icontains=q) | Q(invoice_number__icontains=q)
        try:
            from datetime import datetime as dt
            parsed_date = dt.strptime(q, "%Y-%m-%d").date()
            filters = filters | Q(appointment__appointment_date=parsed_date)
        except ValueError:
            pass
        qs = qs.filter(filters)[:20]
        return Response([
            {
                "invoice_id": inv.id,
                "invoice_number": inv.invoice_number,
                "patient_name": f"{inv.patient.first_name} {inv.patient.last_name}",
                "patient_payment_profile": (inv.patient.payment_profile or "").strip(),
                "date_of_service": str(inv.appointment.appointment_date),
                "total_amount": str(inv.total_amount),
                "status": inv.status,
            }
            for inv in qs
        ])

    @action(detail=True, methods=["get"], url_path="consultation_diagnoses")
    def consultation_diagnoses(self, request, pk=None):
        """Catalog diagnosis IDs for this consultation — prior visit prefill when this visit has none yet."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        appointment = (
            Appointment.objects.filter(pk=pk, provider=provider)
            .select_related("booked_service")
            .first()
        )
        if not appointment:
            return Response({"detail": "Appointment not found."}, status=status.HTTP_404_NOT_FOUND)
        if appointment.status not in (
            Appointment.Status.IN_CONSULTATION,
            Appointment.Status.CHECKED_IN,
        ):
            return Response(
                {"detail": "Diagnosis prefill is only available for active consultations."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(consultation_workspace_for_appointment(appointment))

    @action(detail=False, methods=["patch"], url_path="appointment_soap_notes")
    def appointment_soap_notes(self, request):
        """Save consultation (SOAP) notes during or after the visit (not billing)."""
        return _save_appointment_soap_notes(request)

    @action(detail=True, methods=["get"], url_path="visit_soap_notes")
    def visit_soap_notes(self, request, pk=None):
        """Load saved SOAP notes for edit after complete visit or while awaiting payment."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        appointment = Appointment.objects.filter(pk=pk, provider=provider).first()
        visit, err = _soap_notes_edit_context(request, appointment)
        if err:
            return err
        return Response({"doctor_notes": visit.doctor_notes or ""})

    @action(detail=True, methods=["post"])
    def start_visit(self, request, pk=None):
        """Start consultation for an appointment (doctor must own it)."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        appointment = Appointment.objects.filter(pk=pk, provider=provider).first()
        if not appointment:
            return Response({"detail": "Appointment not found."}, status=status.HTTP_404_NOT_FOUND)
        if appointment.status == Appointment.Status.IN_CONSULTATION:
            visit, _ = Visit.objects.get_or_create(
                appointment=appointment,
                defaults={"patient": appointment.patient, "provider": provider, "status": Visit.Status.IN_PROGRESS},
            )
            return Response({"visit_id": visit.id})
        if appointment.status != Appointment.Status.CHECKED_IN:
            return Response(
                {
                    "detail": "Check the patient in (kiosk or front desk) before starting the visit.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        appointment.status = Appointment.Status.IN_CONSULTATION
        appointment.consultation_started_at = timezone.now()
        appointment.save(update_fields=["status", "consultation_started_at", "updated_at"])
        visit, _ = Visit.objects.get_or_create(
            appointment=appointment,
            defaults={"patient": appointment.patient, "provider": provider, "status": Visit.Status.IN_PROGRESS},
        )
        if visit.status == Visit.Status.OPEN:
            visit.status = Visit.Status.IN_PROGRESS
            visit.save(update_fields=["status", "updated_at"])
        return Response({"visit_id": visit.id})

    @action(detail=True, methods=["post"])
    def complete_visit(self, request, pk=None):
        """Complete a visit (doctor must own it). pk = appointment_id. Body: doctor_notes, diagnosis, rendered_services[]."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        visit = Visit.objects.filter(appointment_id=pk, provider=provider).select_related("appointment__booked_service").first()
        if not visit:
            return Response({"detail": "Visit not found."}, status=status.HTTP_404_NOT_FOUND)
        if visit.appointment.status != Appointment.Status.IN_CONSULTATION:
            return Response(
                {
                    "detail": (
                        "This visit is not in progress. To add services or change billing while waiting on payment, "
                        "tap Edit billing on that appointment."
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        ser = DoctorCompleteVisitSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        rendered_payload = []
        for line in data["rendered_services"]:
            svc = Service.objects.get(pk=line["service_id"])
            if not svc.is_active or not svc.visible_for_primary_service_type(provider.primary_service_type):
                return Response(
                    {
                        "detail": (
                            f'Service "{svc.name}" is not available for your provider type '
                            "or is inactive. Refresh the visit page and pick from the allowed list."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            unit = line["unit_price"] if line.get("unit_price") is not None else svc.price
            rendered_payload.append(
                {
                    "service_id": svc.id,
                    "quantity": line.get("quantity", 1),
                    "unit_price": str(unit),
                }
            )
        payload = _complete_visit_payload_from_validated(data, rendered_payload)
        invoice = complete_visit_with_services(visit, payload)
        visit.appointment.status = Appointment.Status.AWAITING_PAYMENT
        visit.appointment.completed_at = timezone.now()
        visit.appointment.save(update_fields=["status", "completed_at", "updated_at"])

        invoice.refresh_from_db()
        followup = _doctor_collect_payment_followup(
            invoice, try_saved_card=data.get("charge_saved_card_if_present", True)
        )
        invoice.refresh_from_db()
        if invoice.status != Invoice.Status.PAID:
            terminal_checkout_id = try_push_terminal_checkout_to_kiosk(invoice)
            if terminal_checkout_id:
                followup["terminal_checkout_id"] = terminal_checkout_id
        return Response(followup)

    @action(detail=True, methods=["get"], url_path="billing_for_edit")
    def billing_for_edit(self, request, pk=None):
        """Load current chart + line items so the doctor can revise billing (awaiting payment or completed)."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        appointment = Appointment.objects.filter(pk=pk, provider=provider).first()
        if not appointment:
            return Response({"detail": "Appointment not found."}, status=status.HTTP_404_NOT_FOUND)
        payload, resp = _visit_billing_for_edit_payload(appointment, provider_id=provider.id)
        if resp:
            return resp
        return Response(payload)

    @action(detail=True, methods=["post"], url_path="revise_visit_billing")
    def revise_visit_billing(self, request, pk=None):
        """Update visit lines and invoice while awaiting payment or after visit completed."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        visit = Visit.objects.filter(appointment_id=pk, provider=provider).select_related("appointment").first()
        if not visit:
            return Response({"detail": "Visit not found."}, status=status.HTTP_404_NOT_FOUND)
        ser = DoctorCompleteVisitSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        rendered_payload = []
        for line in data["rendered_services"]:
            svc = Service.objects.get(pk=line["service_id"])
            if not svc.is_active or not svc.visible_for_primary_service_type(provider.primary_service_type):
                return Response(
                    {
                        "detail": (
                            f'Service "{svc.name}" is not available for your provider type '
                            "or is inactive. Refresh and pick from the allowed list."
                        ),
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            unit = line["unit_price"] if line.get("unit_price") is not None else svc.price
            rendered_payload.append(
                {
                    "service_id": svc.id,
                    "quantity": line.get("quantity", 1),
                    "unit_price": str(unit),
                }
            )
        payload = _complete_visit_payload_from_validated(data, rendered_payload)
        try:
            invoice = revise_visit_billing_admin(visit, payload)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        invoice.refresh_from_db()
        followup = _doctor_collect_payment_followup(
            invoice, try_saved_card=data.get("charge_saved_card_if_present", False)
        )
        invoice.refresh_from_db()
        if invoice.status != Invoice.Status.PAID:
            terminal_checkout_id = try_push_terminal_checkout_to_kiosk(invoice)
            if terminal_checkout_id:
                followup["terminal_checkout_id"] = terminal_checkout_id
        return Response(followup)

    @action(detail=False, methods=["post"], url_path="prepare_invoice_payment")
    def prepare_invoice_payment(self, request):
        """Re-open payment options for a visit stuck in awaiting_payment (e.g. banner was dismissed)."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        raw_aid = request.data.get("appointment_id")
        if raw_aid is None:
            return Response({"detail": "appointment_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            appointment_id = int(raw_aid)
        except (TypeError, ValueError):
            return Response({"detail": "Invalid appointment_id."}, status=status.HTTP_400_BAD_REQUEST)

        appt = Appointment.objects.filter(pk=appointment_id, provider=provider).first()
        if not appt:
            return Response({"detail": "Appointment not found."}, status=status.HTTP_404_NOT_FOUND)
        from .invoice_collection import open_invoice_for_appointment_payment

        inv = open_invoice_for_appointment_payment(appt)
        if not inv:
            return Response(
                {
                    "detail": (
                        "No unpaid invoice on file for this visit. "
                        "If this was a no-show, confirm a fee was applied in Admin → Settings."
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        inv = Invoice.objects.filter(pk=inv.pk).select_related("patient").first()

        if inv.status != Invoice.Status.PAID:
            try_reconcile_invoice_from_square(inv)
            inv.refresh_from_db()
            appt.refresh_from_db()

        if inv.status == Invoice.Status.PAID:
            return Response(
                {
                    "invoice_id": inv.id,
                    "invoice_number": inv.invoice_number,
                    "total_amount": str(inv.total_amount),
                    "patient_credit_balance": str(inv.patient.credit_balance),
                    "already_paid": True,
                    "payment": {
                        "status": "charged_saved_card",
                        "charged": True,
                        "checkout_url": None,
                        "charge_error": None,
                        "payment_intent_id": None,
                    },
                }
            )

        try_saved_card = bool(request.data.get("try_saved_card", False))
        followup = _doctor_collect_payment_followup(inv, try_saved_card=try_saved_card)
        return Response(followup)

    @action(detail=False, methods=["get"], url_path="square_terminal_config")
    def square_terminal_config(self, request):
        """Doctor UI: Square location + whether a Terminal device id is configured."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        if not square_configured():
            return Response({"detail": "Square is not configured."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        loc = get_location_id()
        dev = get_terminal_device_id()
        return Response(
            {
                "location_id": loc,
                "has_location": bool(loc),
                "device_id_configured": bool(dev),
            }
        )

    @action(detail=False, methods=["get"], url_path="square_pos_config")
    def square_pos_config(self, request):
        """Whether Square POS (Stand + reader via Square POS app) launch URLs can be built."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        if not square_configured():
            return Response({"detail": "Square is not configured."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(
            {
                "pos_callback_configured": pos_callback_configured(),
                "has_location": bool(get_location_id()),
                "has_application_id": bool(get_application_id()),
            }
        )

    @action(detail=False, methods=["get"], url_path="square_pos_launch")
    def square_pos_launch(self, request):
        """
        Returns URLs to open Square Point of Sale on iPad/Android (tap/insert card on reader).
        Register SQUARE_POS_CALLBACK_URL in .env and in Square Developer Console (POS web callback).
        """
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        if not square_configured():
            return Response({"detail": "Square is not configured."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        if not pos_callback_configured():
            return Response(
                {"detail": "Square POS callback URL is not configured (SQUARE_POS_CALLBACK_URL)."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        ser = TerminalCheckoutSerializer(data={"invoice_id": request.query_params.get("invoice_id")})
        ser.is_valid(raise_exception=True)
        inv = (
            Invoice.objects.select_related("appointment")
            .filter(pk=ser.validated_data["invoice_id"])
            .first()
        )
        if not inv:
            return Response({"detail": "Invoice not found."}, status=status.HTTP_404_NOT_FOUND)
        if inv.appointment.provider_id != provider.id:
            return Response({"detail": "Not allowed."}, status=status.HTTP_403_FORBIDDEN)
        if inv.status != Invoice.Status.ISSUED:
            return Response(
                {"detail": "Invoice is not awaiting payment."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            ios_url = build_ios_square_pos_url(inv)
            android_intent = build_android_square_pos_intent(inv)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"ios_url": ios_url, "android_intent_url": android_intent})

    @action(detail=False, methods=["post"], url_path="terminal_checkout")
    def terminal_checkout(self, request):
        """Create a Square Terminal checkout (in-person) for an unpaid invoice."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        if not square_configured():
            return Response({"detail": "Square is not configured."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        ser = TerminalCheckoutSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        inv = (
            Invoice.objects.select_related("appointment")
            .filter(pk=ser.validated_data["invoice_id"])
            .first()
        )
        if not inv:
            return Response({"detail": "Invoice not found."}, status=status.HTTP_404_NOT_FOUND)
        if inv.appointment.provider_id != provider.id:
            return Response({"detail": "Not allowed."}, status=status.HTTP_403_FORBIDDEN)
        if inv.status not in (Invoice.Status.ISSUED, Invoice.Status.OVERDUE, Invoice.Status.DRAFT):
            return Response(
                {"detail": "Invoice is not awaiting payment."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        include_pending = bool(ser.validated_data.get("include_pending_fees"))
        try:
            out = create_terminal_checkout_for_invoice(inv, include_pending_fees=include_pending)
        except Exception as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(out)

    @action(detail=False, methods=["get"], url_path="terminal_checkout_status")
    def terminal_checkout_status(self, request):
        """Poll Terminal checkout status; marks invoice paid when checkout completes."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        if not square_configured():
            return Response({"detail": "Square is not configured."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        ser = TerminalCheckoutStatusSerializer(data=request.query_params)
        ser.is_valid(raise_exception=True)
        cid = ser.validated_data["checkout_id"]
        try:
            out = get_terminal_checkout_status(cid)
        except Exception as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(out)

    @action(detail=False, methods=["get"], url_path="google_calendar/status")
    def google_calendar_status(self, request):
        """Whether server OAuth is configured and this doctor has connected a personal Google account."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        connected = bool((provider.google_refresh_token or "").strip())
        return Response(
            {
                "oauth_configured": google_oauth_configured(),
                "connected": connected,
            }
        )

    @action(detail=False, methods=["get"], url_path="google_calendar/oauth/start")
    def google_calendar_oauth_start(self, request):
        """Returns Google authorization URL (doctor opens in browser to connect personal Calendar)."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        if not google_oauth_configured():
            return Response(
                {"detail": "Google Calendar OAuth is not configured on the server."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        try:
            flow = build_oauth_flow()
        except Exception as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        signer = TimestampSigner(salt="google-calendar-oauth")
        state = signer.sign(str(request.user.id))
        authorization_url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            include_granted_scopes="true",
            state=state,
        )
        return Response({"authorization_url": authorization_url})

    @action(
        detail=False,
        methods=["get"],
        url_path="google_calendar/oauth/callback",
        permission_classes=[permissions.AllowAny],
        authentication_classes=[],
    )
    def google_calendar_oauth_callback(self, request):
        """OAuth redirect target (no JWT). State carries signed doctor user id."""
        from urllib.parse import urlencode

        base = getattr(settings, "FRONTEND_BASE_URL", "http://localhost:3000").rstrip("/")

        def redir(params: dict):
            return HttpResponseRedirect(f"{base}/doctor/schedule?{urlencode(params)}")

        if not google_oauth_configured():
            return redir({"google_calendar": "error", "reason": "config"})
        err = request.query_params.get("error")
        if err:
            return redir({"google_calendar": "error", "reason": err})
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        if not code or not state:
            return redir({"google_calendar": "error", "reason": "missing_code"})
        try:
            exchange_oauth_code(
                authorization_response_url=request.build_absolute_uri(),
                state=state,
            )
        except ValueError as exc:
            return redir({"google_calendar": "error", "reason": str(exc)[:120]})
        except Exception:
            return redir({"google_calendar": "error", "reason": "oauth_failed"})
        return redir({"google_calendar": "connected"})

    @action(detail=False, methods=["post"], url_path="google_calendar/disconnect")
    def google_calendar_disconnect(self, request):
        """Remove stored Google tokens for this doctor (events are not deleted from Google)."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        provider.google_refresh_token = ""
        provider.save(update_fields=["google_refresh_token", "updated_at"])
        return Response({"detail": "Disconnected from Google Calendar."})


class StaffNotificationViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """In-app bell: list + unread count + mark read (recipient = current user)."""

    serializer_class = StaffNotificationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return StaffNotification.objects.filter(recipient=self.request.user).order_by("-created_at")

    def list(self, request, *args, **kwargs):
        qs = self.filter_queryset(self.get_queryset())[:40]
        serializer = self.get_serializer(qs, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=["get"], url_path="unread-count")
    def unread_count(self, request):
        n = StaffNotification.objects.filter(recipient=request.user, read_at__isnull=True).count()
        return Response({"unread_count": n})

    @action(detail=True, methods=["post"])
    def mark_read(self, request, pk=None):
        n = get_object_or_404(StaffNotification, pk=pk, recipient=request.user)
        if n.read_at is None:
            n.read_at = timezone.now()
            n.save(update_fields=["read_at", "updated_at"])
        return Response(StaffNotificationSerializer(n).data)

    @action(detail=False, methods=["post"], url_path="mark_all_read")
    def mark_all_read(self, request):
        StaffNotification.objects.filter(recipient=request.user, read_at__isnull=True).update(
            read_at=timezone.now()
        )
        return Response({"detail": "ok"})


class KioskViewSet(viewsets.ViewSet):
    """Public kiosk: patient lookup and check-in by phone (supports shared household numbers)."""

    permission_classes = [permissions.AllowAny]

    @staticmethod
    def _kiosk_appointments_qs(patient: Patient):
        return (
            Appointment.objects.filter(patient=patient)
            .exclude(
                status__in=[Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW],
            )
            .select_related("patient", "provider", "booked_service")
            .order_by("appointment_date", "start_time")
        )

    @classmethod
    def _kiosk_visit_line(cls, appt: Appointment) -> dict:
        service_name = ""
        if appt.booked_service_id and appt.booked_service:
            service_name = (appt.booked_service.name or "").strip()
        return {
            "appointment_id": appt.id,
            "start_time_display": format_time_12h(appt.start_time),
            "provider": str(appt.provider),
            "service_name": service_name,
        }

    @classmethod
    def _kiosk_appointment_choice(cls, appt: Appointment) -> dict:
        """One kiosk row per appointment — patient checks in separately for each visit."""
        patient_name = f"{appt.patient.first_name} {appt.patient.last_name}".strip()
        line = cls._kiosk_visit_line(appt)
        allowed, _, earliest_local = cls._can_kiosk_checkin_now(appt)
        payload = {
            "appointment_id": appt.id,
            "patient": patient_name,
            "provider": line["provider"],
            "start_time_display": line["start_time_display"],
            "service_name": line.get("service_name") or "",
            "can_checkin": allowed,
            "early_checkin_minutes_before": cls._kiosk_minutes_before(),
        }
        if not allowed and earliest_local is not None:
            payload["earliest_checkin_display"] = format_time_12h(earliest_local.time())
        return payload

    @classmethod
    def _kiosk_resolve_checkin_targets(
        cls,
        primary: Appointment,
        *,
        requested_ids: list[int] | None,
        bypass_early: bool,
    ) -> tuple[list[Appointment], str | None]:
        """Visits to mark checked-in — only the appointment(s) explicitly requested (no same-day batching)."""
        if requested_ids:
            if len(requested_ids) > 1:
                return [], "Check in one appointment at a time."
            qs = list(
                Appointment.objects.filter(pk__in=requested_ids).select_related(
                    "patient", "provider", "booked_service"
                )
            )
            if len(qs) != len(set(requested_ids)):
                return [], "One or more appointments were not found."
            if len({a.patient_id for a in qs}) > 1:
                return [], "All appointments must be for the same patient."
            if any(a.appointment_date != primary.appointment_date for a in qs):
                return [], "All appointments must be on the same day."
            if any(a.patient_id != primary.patient_id for a in qs):
                return [], "Appointments do not match this patient."
            targets = sorted(qs, key=lambda a: (a.start_time, a.pk))
        else:
            targets = [primary]

        booked = [a for a in targets if a.status == Appointment.Status.BOOKED]
        if not booked:
            return [], "Check-in is already done or this visit is in progress."

        if not bypass_early:
            too_early = [a for a in booked if not cls._can_kiosk_checkin_now(a)[0]]
            if too_early:
                minutes = cls._kiosk_minutes_before()
                appt = too_early[0]
                _, _, earliest_local = cls._can_kiosk_checkin_now(appt)
                earliest_disp = format_time_12h(earliest_local.time()) if earliest_local else ""
                return [], (
                    f"It is too early for kiosk check-in. You can check in up to {minutes} minutes "
                    f"before this appointment"
                    + (f" (opens around {earliest_disp})" if earliest_disp else "")
                    + ", or ask the front desk."
                )

        return booked, None

    @classmethod
    def _kiosk_minutes_before(cls) -> int:
        v = getattr(settings, "KIOSK_EARLY_CHECKIN_MINUTES_BEFORE", 30)
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return 30

    @staticmethod
    def _desk_can_bypass_kiosk_early_window(request) -> bool:
        """Logged-in admin/doctor/staff may check in from the schedule before the kiosk window opens."""
        return bool(
            request.user
            and request.user.is_authenticated
            and getattr(request.user, "role", None) in ("owner_admin", "doctor", "staff")
        )

    @classmethod
    def _can_kiosk_checkin_now(cls, appt: Appointment):
        """Return (allowed, start_local, earliest_local) in clinic timezone."""
        from apps.clinic.timezone_utils import get_clinic_timezone, now_clinic

        tz = get_clinic_timezone()
        start_local = datetime.combine(appt.appointment_date, appt.start_time, tzinfo=tz)
        minutes = cls._kiosk_minutes_before()
        earliest_local = start_local - timedelta(minutes=minutes)
        return now_clinic() >= earliest_local, start_local, earliest_local

    @staticmethod
    def _today_appointments_for_phone(norm: str) -> tuple[list[Patient], list[Appointment], list[Appointment]]:
        """All patients on this number, their today appts, and all non-cancelled appts (for future lookup)."""
        from apps.clinic.patient_phone import patients_matching_phone
        from apps.clinic.timezone_utils import today_clinic

        today = today_clinic()
        patients = patients_matching_phone(norm)
        if not patients:
            return [], [], []

        all_appts: list[Appointment] = []
        today_appts: list[Appointment] = []
        for patient in patients:
            qs = list(KioskViewSet._kiosk_appointments_qs(patient))
            all_appts.extend(qs)
            today_appts.extend(a for a in qs if a.appointment_date == today)
        today_appts.sort(key=lambda a: (a.start_time, a.pk))
        all_appts.sort(key=lambda a: (a.appointment_date, a.start_time, a.pk))
        return patients, today_appts, all_appts

    @action(detail=False, methods=["post"])
    def lookup(self, request):
        phone = request.data.get("phone")
        from apps.clinic.timezone_utils import today_clinic

        today = today_clinic()
        valid, msg = validate_phone(phone or "")
        if not valid:
            return Response(
                {"result": "invalid_phone", "message": msg or "Invalid phone."},
            )
        norm = normalize_phone(phone)
        patients, today_appts, all_appts = self._today_appointments_for_phone(norm)
        if not patients:
            return Response(
                {
                    "result": "not_found",
                    "message": (
                        "We could not find an appointment for this phone number. "
                        "If you have not booked yet, you can book online or ask the front desk for help."
                    ),
                }
            )

        open_today = [a for a in today_appts if a.status == Appointment.Status.BOOKED]
        if open_today:
            open_today.sort(key=lambda a: (a.start_time, a.pk))
            if len(open_today) == 1:
                appt = open_today[0]
                choice = self._kiosk_appointment_choice(appt)
                patient_name = choice["patient"]
                if choice["can_checkin"]:
                    return Response(
                        {
                            "result": "ready",
                            "appointment_id": appt.id,
                            "patient": patient_name,
                            "provider": choice["provider"],
                            "time": str(appt.start_time),
                            "start_time_display": choice["start_time_display"],
                            "service_name": choice.get("service_name") or "",
                            "status": appt.status,
                        }
                    )
                minutes = self._kiosk_minutes_before()
                return Response(
                    {
                        "result": "too_early",
                        "message": (
                            f"You are here before your check-in window. Kiosk check-in opens up to "
                            f"{minutes} minutes before your appointment, or ask the front desk if you need help."
                        ),
                        "appointment_id": appt.id,
                        "patient": patient_name,
                        "provider": choice["provider"],
                        "start_time_display": choice["start_time_display"],
                        "earliest_checkin_display": choice.get("earliest_checkin_display", ""),
                        "early_checkin_minutes_before": minutes,
                    }
                )

            choices = [self._kiosk_appointment_choice(a) for a in open_today]
            patient_names = list(dict.fromkeys(c["patient"] for c in choices))
            if len(patient_names) > 1:
                message = (
                    "We found more than one person with appointments on this phone number today. "
                    "Please tap the visit you are checking in for."
                )
            else:
                message = (
                    f"You have {len(choices)} appointments today. "
                    "Please tap the visit you are checking in for."
                )
            return Response(
                {
                    "result": "choose_appointment",
                    "message": message,
                    "choices": choices,
                }
            )

        in_progress_today = [
            a
            for a in today_appts
            if a.status
            in (
                Appointment.Status.CHECKED_IN,
                Appointment.Status.IN_CONSULTATION,
                Appointment.Status.AWAITING_PAYMENT,
            )
        ]
        if in_progress_today:
            if len(in_progress_today) > 1:
                names = ", ".join(
                    f"{a.patient.first_name} {a.patient.last_name}" for a in in_progress_today
                )
                detail = (
                    f"You are already checked in for a visit today ({names}). "
                    "If you have another appointment later, check in again when it is time for that visit."
                )
            else:
                detail = (
                    "Our records show you have already completed check-in for this visit. "
                    "Please have a seat in the waiting area — we will call you when it is time. "
                    "If you have another appointment later today, check in again when it is time for that visit."
                )
            appt = min(in_progress_today, key=lambda a: (a.start_time, a.pk))
            return Response(
                {
                    "result": "already_checked_in",
                    "message": detail,
                    "patient": f"{appt.patient.first_name} {appt.patient.last_name}",
                    "start_time_display": format_time_12h(appt.start_time),
                }
            )

        if today_appts and all(a.status == Appointment.Status.COMPLETED for a in today_appts):
            return Response(
                {
                    "result": "visit_completed_today",
                    "message": (
                        "It looks like your visit for today is already completed. "
                        "If you need another appointment, please book online or speak with the front desk."
                    ),
                }
            )

        future = [a for a in all_appts if a.appointment_date > today]
        if future:
            nxt = min(future, key=lambda a: (a.appointment_date, a.start_time))
            date_disp = nxt.appointment_date.strftime("%A, %B %d, %Y")
            who = f"{nxt.patient.first_name} {nxt.patient.last_name}"
            return Response(
                {
                    "result": "wrong_day",
                    "message": (
                        f"We do not see an appointment for today on this number. "
                        f"The next visit is for {who} on {date_disp} at {format_time_12h(nxt.start_time)}."
                    ),
                    "appointment_date_display": date_disp,
                    "start_time_display": format_time_12h(nxt.start_time),
                    "patient": who,
                }
            )

        return Response(
            {
                "result": "not_found",
                "message": (
                    "We could not find an upcoming appointment for this phone number. "
                    "You can book online or ask the front desk for help."
                ),
            }
        )

    @action(detail=False, methods=["post"])
    def checkin(self, request):
        import logging

        from apps.clinic.patient_phone import patient_matches_phone_normalized
        from apps.clinic.timezone_utils import today_clinic

        checkin_logger = logging.getLogger(__name__)

        try:
            appointment_id = request.data.get("appointment_id")
            raw_ids = request.data.get("appointment_ids")
            if appointment_id is None and not raw_ids:
                return Response(
                    {"detail": "appointment_id or appointment_ids is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if appointment_id is None and raw_ids:
                try:
                    appointment_id = int(raw_ids[0])
                except (TypeError, ValueError, IndexError):
                    return Response(
                        {"detail": "appointment_ids must be a non-empty list of numbers."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            try:
                primary_id = int(appointment_id)
            except (TypeError, ValueError):
                return Response(
                    {"detail": "appointment_id must be a number."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            requested_ids: list[int] | None = None
            if raw_ids is not None:
                if not isinstance(raw_ids, (list, tuple)):
                    return Response(
                        {"detail": "appointment_ids must be a list."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                try:
                    requested_ids = [int(x) for x in raw_ids]
                except (TypeError, ValueError):
                    return Response(
                        {"detail": "appointment_ids must be a list of numbers."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if not requested_ids:
                    return Response(
                        {"detail": "appointment_ids cannot be empty."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

            appt = get_object_or_404(
                Appointment.objects.select_related("patient", "provider", "booked_service"),
                pk=primary_id,
            )
            phone = request.data.get("phone")
            if phone:
                valid, msg = validate_phone(phone)
                if not valid:
                    return Response({"detail": msg}, status=status.HTTP_400_BAD_REQUEST)
                if not patient_matches_phone_normalized(appt.patient, normalize_phone(phone)):
                    return Response(
                        {"detail": "This appointment does not match that phone number."},
                        status=status.HTTP_403_FORBIDDEN,
                    )
            today = today_clinic()
            if appt.appointment_date != today:
                return Response(
                    {"detail": "Check-in is only available on the day of your appointment."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            bypass_early = not phone and self._desk_can_bypass_kiosk_early_window(request)

            targets, err = self._kiosk_resolve_checkin_targets(
                appt,
                requested_ids=requested_ids,
                bypass_early=bypass_early,
            )
            if err:
                return Response({"detail": err}, status=status.HTTP_400_BAD_REQUEST)

            now = timezone.now()
            checked_ids: list[int] = []
            early_err: str | None = None

            def after_checkin_side_effects(appointment_id: int) -> None:
                try:
                    from apps.notifications.tasks import notify_provider_patient_checked_in_task

                    notify_provider_patient_checked_in_task.delay(appointment_id)
                except Exception:
                    checkin_logger.exception(
                        "check-in: failed to queue provider SMS for appointment %s",
                        appointment_id,
                    )
                try:
                    from apps.clinic.in_app_notify import create_checkin_in_app_notification

                    create_checkin_in_app_notification(appointment_id)
                except Exception:
                    checkin_logger.exception(
                        "check-in: failed in-app notification for appointment %s",
                        appointment_id,
                    )

            with transaction.atomic():
                for visit in targets:
                    if early_err:
                        break
                    if visit.status in (
                        Appointment.Status.CANCELLED,
                        Appointment.Status.NO_SHOW,
                        Appointment.Status.COMPLETED,
                    ):
                        continue
                    if visit.status != Appointment.Status.BOOKED:
                        continue
                    if not bypass_early:
                        allowed, _, earliest_local = self._can_kiosk_checkin_now(visit)
                        if not allowed:
                            minutes = self._kiosk_minutes_before()
                            earliest_disp = (
                                format_time_12h(earliest_local.time()) if earliest_local else ""
                            )
                            early_err = (
                                f"It is too early for kiosk check-in. You can check in up to {minutes} minutes "
                                f"before this appointment"
                                + (f" (opens around {earliest_disp})" if earliest_disp else "")
                                + ", or ask the front desk."
                            )
                            break
                    # booked_service is nullable — FOR UPDATE + outer join crashes on Postgres.
                    locked = (
                        Appointment.objects.select_for_update(of=("self",))
                        .filter(pk=visit.pk, status=Appointment.Status.BOOKED)
                        .first()
                    )
                    if not locked:
                        continue
                    locked.status = Appointment.Status.CHECKED_IN
                    locked.checked_in_at = now
                    locked.save(update_fields=["status", "checked_in_at", "updated_at"])
                    checked_ids.append(locked.id)
                    aid = locked.id
                    transaction.on_commit(lambda aid=aid: after_checkin_side_effects(aid))

            if early_err:
                return Response({"detail": early_err}, status=status.HTTP_400_BAD_REQUEST)

            if not checked_ids:
                return Response(
                    {"detail": "Check-in is already done or this visit is in progress."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            lead = (
                Appointment.objects.select_related("patient", "booked_service")
                .filter(pk=checked_ids[0])
                .first()
            ) or appt
            patient_name = f"{lead.patient.first_name} {lead.patient.last_name}".strip()
            service = (lead.booked_service.name if lead.booked_service else "").strip()
            time_disp = format_time_12h(lead.start_time)
            if service:
                detail = f"Checked in for {service} at {time_disp}"
            else:
                detail = f"Checked in for {time_disp}"
            if patient_name:
                detail += f" — {patient_name}"
            detail += "."

            appointments_payload = []
            for row in Appointment.objects.filter(pk__in=checked_ids).select_related("booked_service"):
                svc = (row.booked_service.name if row.booked_service else "").strip()
                appointments_payload.append(
                    {
                        "appointment_id": row.id,
                        "status": row.status,
                        "checked_in_at": row.checked_in_at.isoformat() if row.checked_in_at else "",
                        "service_name": svc,
                        "start_time_display": format_time_12h(row.start_time),
                    }
                )

            return Response(
                {
                    "detail": detail,
                    "status": Appointment.Status.CHECKED_IN,
                    "checked_in_count": len(checked_ids),
                    "appointment_ids": checked_ids,
                    "appointments": appointments_payload,
                }
            )
        except Exception:
            checkin_logger.exception("kiosk checkin failed")
            return Response(
                {"detail": "Check-in could not be completed. Please try again or see the front desk."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
