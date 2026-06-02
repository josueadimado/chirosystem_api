"""Unpaid balances and multi-invoice payment bundles (visit + penalty fees)."""

from __future__ import annotations

from decimal import Decimal

from django.db.models import Sum

from .models import Invoice, Patient
from .patient_demographics import annotate_patient_unpaid_balances

# Match Square settlement / doctor awaiting-payment flows (includes draft visit bills).
_OPEN_INVOICE_STATUSES = (Invoice.Status.ISSUED, Invoice.Status.OVERDUE, Invoice.Status.DRAFT)

_PENALTY_KINDS = (Invoice.Kind.NO_SHOW_FEE, Invoice.Kind.LATE_CANCEL_FEE)

_KIND_LABELS = {
    Invoice.Kind.VISIT: "Visit",
    Invoice.Kind.NO_SHOW_FEE: "No-show fee",
    Invoice.Kind.LATE_CANCEL_FEE: "Late cancel fee",
}


def open_penalty_invoices_for_patient(patient_id: int, *, exclude_invoice_id: int | None = None):
    qs = (
        Invoice.objects.filter(
            patient_id=patient_id,
            kind__in=_PENALTY_KINDS,
            status__in=_OPEN_INVOICE_STATUSES,
        )
        .select_related("appointment")
        .order_by("created_at")
    )
    if exclude_invoice_id:
        qs = qs.exclude(pk=exclude_invoice_id)
    return qs


def patient_balance_due_by_id(patient_ids: list[int]) -> dict[int, str]:
    """Total unpaid across all invoice kinds, keyed by patient id."""
    if not patient_ids:
        return {}
    rows = (
        Invoice.objects.filter(
            patient_id__in=patient_ids,
            status__in=_OPEN_INVOICE_STATUSES,
        )
        .values("patient_id")
        .annotate(total=Sum("total_amount"))
    )
    out = {pid: "0.00" for pid in patient_ids}
    for row in rows:
        pid = row["patient_id"]
        total = row["total"] or Decimal("0")
        out[pid] = str(total.quantize(Decimal("0.01")))
    return out


def _money_str(d: Decimal) -> str:
    return str(d.quantize(Decimal("0.01")))


def _serialize_pending_invoice(inv: Invoice) -> dict:
    appt = inv.appointment
    return {
        "id": inv.id,
        "invoice_number": inv.invoice_number,
        "kind": inv.kind,
        "kind_label": _KIND_LABELS.get(inv.kind, inv.kind),
        "total_amount": _money_str(Decimal(inv.total_amount or 0)),
        "appointment_date": str(appt.appointment_date) if appt else None,
    }


def build_doctor_pending_payment_context(
    patient_id: int,
    *,
    current_invoice_id: int | None = None,
) -> dict:
    """
    Context for doctor UI: other open penalty bills, optional current visit invoice, combined total.
    """
    annotated = annotate_patient_unpaid_balances(Patient.objects.filter(pk=patient_id)).first()
    bal_visit = Decimal(getattr(annotated, "balance_visit", None) or 0)
    bal_no_show = Decimal(getattr(annotated, "balance_no_show_fee", None) or 0)
    bal_late = Decimal(getattr(annotated, "balance_late_cancel_fee", None) or 0)
    balance_total = (bal_visit + bal_no_show + bal_late).quantize(Decimal("0.01"))
    balance_penalties = (bal_no_show + bal_late).quantize(Decimal("0.01"))

    current_amount = Decimal("0")
    if current_invoice_id:
        cur = Invoice.objects.filter(pk=current_invoice_id, patient_id=patient_id).first()
        if cur and cur.status in _OPEN_INVOICE_STATUSES:
            current_amount = Decimal(cur.total_amount or 0)

    other_invoices = list(open_penalty_invoices_for_patient(patient_id, exclude_invoice_id=current_invoice_id))
    other_total = sum((Decimal(i.total_amount or 0) for i in other_invoices), Decimal("0")).quantize(Decimal("0.01"))
    combined = (current_amount + other_total).quantize(Decimal("0.01"))

    return {
        "balance_total": _money_str(balance_total),
        "balance_penalties": _money_str(balance_penalties),
        "has_other_pending": other_total > 0,
        "other_invoices": [_serialize_pending_invoice(i) for i in other_invoices],
        "other_total": _money_str(other_total),
        "current_amount": _money_str(current_amount),
        "combined_amount": _money_str(combined),
    }


def invoice_ids_for_doctor_bundle(primary_invoice: Invoice, *, include_pending_fees: bool) -> list[int]:
    """Primary visit invoice first, then open penalty invoices (oldest first)."""
    ids = [primary_invoice.pk]
    if not include_pending_fees:
        return ids
    ids.extend(
        open_penalty_invoices_for_patient(
            primary_invoice.patient_id,
            exclude_invoice_id=primary_invoice.pk,
        ).values_list("pk", flat=True)
    )
    return ids


def combined_total_for_invoice_ids(invoice_ids: list[int]) -> Decimal:
    if not invoice_ids:
        return Decimal("0")
    total = Decimal("0")
    for inv in Invoice.objects.filter(pk__in=invoice_ids).only("total_amount", "status"):
        if inv.status in _OPEN_INVOICE_STATUSES:
            total += Decimal(inv.total_amount or 0)
    return total.quantize(Decimal("0.01"))
