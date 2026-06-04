"""When staff can collect payment on an appointment invoice (visit or penalty fees)."""

from __future__ import annotations

from decimal import Decimal

from django.utils import timezone

from apps.clinic.models import Appointment, Invoice, Payment

_OPEN_INVOICE_STATUSES = frozenset(
    {
        Invoice.Status.ISSUED,
        Invoice.Status.OVERDUE,
        Invoice.Status.DRAFT,
    }
)

_COLLECTIBLE_APPOINTMENT_STATUSES = frozenset(
    {
        Appointment.Status.AWAITING_PAYMENT,
        Appointment.Status.NO_SHOW,
        Appointment.Status.CANCELLED,
    }
)


def open_invoice_for_appointment_payment(appointment: Appointment) -> Invoice | None:
    """
    Unpaid invoice this appointment can be paid against at the desk or on Terminal.

    - Visit bills: appointment awaiting_payment.
    - No-show fee: status no_show + invoice kind no_show_fee.
    - Late-cancel fee: status cancelled + invoice kind late_cancel_fee.
    """
    if appointment.status not in _COLLECTIBLE_APPOINTMENT_STATUSES:
        return None
    inv = (
        Invoice.objects.filter(appointment=appointment)
        .order_by("-created_at")
        .first()
    )
    if not inv or inv.status not in _OPEN_INVOICE_STATUSES:
        return None
    if appointment.status == Appointment.Status.AWAITING_PAYMENT:
        return inv
    if appointment.status == Appointment.Status.NO_SHOW and inv.kind == Invoice.Kind.NO_SHOW_FEE:
        return inv
    if appointment.status == Appointment.Status.CANCELLED and inv.kind == Invoice.Kind.LATE_CANCEL_FEE:
        return inv
    return None


def _is_credit_ledger_payment(payment: Payment) -> bool:
    """Credit applied via apply_credit — already reflected in invoice.total_amount."""
    return (payment.payment_reference or "").startswith("patient_credit:")


def invoice_cash_and_card_paid_total(invoice: Invoice) -> Decimal:
    """Sum of successful desk/card/cash payments (excludes patient-credit ledger rows)."""
    paid = Decimal("0")
    for p in invoice.payments.filter(status=Payment.Status.SUCCESSFUL):
        if _is_credit_ledger_payment(p):
            continue
        paid += Decimal(p.amount or 0)
    return paid.quantize(Decimal("0.01"))


def invoice_amount_due(invoice: Invoice) -> Decimal:
    """Client amount still owed on this invoice (supports partial cash payments)."""
    total = Decimal(invoice.total_amount or 0).quantize(Decimal("0.01"))
    paid = invoice_cash_and_card_paid_total(invoice)
    due = (total - paid).quantize(Decimal("0.01"))
    return due if due > Decimal("0") else Decimal("0.00")


def set_appointment_status_after_invoice_paid(inv: Invoice) -> None:
    """Keep no-show / late-cancel labels when penalty invoices are paid; complete normal visits."""
    appt = inv.appointment
    if inv.kind == Invoice.Kind.NO_SHOW_FEE:
        target_status = Appointment.Status.NO_SHOW
    elif inv.kind == Invoice.Kind.LATE_CANCEL_FEE:
        target_status = Appointment.Status.CANCELLED
    else:
        target_status = Appointment.Status.COMPLETED
    if appt.status != target_status:
        appt.status = target_status
        if target_status == Appointment.Status.CANCELLED:
            appt.completed_at = None
        elif not appt.completed_at:
            appt.completed_at = timezone.now()
        appt.save(update_fields=["status", "completed_at", "updated_at"])


def invoice_payment_summary(invoice: Invoice) -> dict[str, str]:
    total = Decimal(invoice.total_amount or 0).quantize(Decimal("0.01"))
    paid = invoice_cash_and_card_paid_total(invoice)
    due = invoice_amount_due(invoice)
    return {
        "invoice_total": str(total),
        "amount_paid": str(paid),
        "amount_due": str(due),
    }
