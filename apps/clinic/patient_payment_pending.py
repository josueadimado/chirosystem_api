"""Unpaid balances and multi-invoice payment bundles (visit + penalty fees)."""

from __future__ import annotations

from decimal import Decimal

from django.db.models import Prefetch

from .invoice_collection import invoice_amount_due
from .models import Invoice, Payment

# Match Square settlement / doctor awaiting-payment flows (includes draft visit bills).
_OPEN_INVOICE_STATUSES = (Invoice.Status.ISSUED, Invoice.Status.OVERDUE, Invoice.Status.DRAFT)

_PENALTY_KINDS = (Invoice.Kind.NO_SHOW_FEE, Invoice.Kind.LATE_CANCEL_FEE)

_KIND_LABELS = {
    Invoice.Kind.VISIT: "Visit",
    Invoice.Kind.NO_SHOW_FEE: "No-show fee",
    Invoice.Kind.LATE_CANCEL_FEE: "Late cancel fee",
}


def _open_invoices_for_patient(patient_id: int):
    return Invoice.objects.filter(
        patient_id=patient_id,
        status__in=_OPEN_INVOICE_STATUSES,
    ).prefetch_related(
        Prefetch(
            "payments",
            queryset=Payment.objects.filter(status=Payment.Status.SUCCESSFUL),
        ),
    )


def open_penalty_invoices_for_patient(patient_id: int, *, exclude_invoice_id: int | None = None):
    """Open no-show / late-cancel invoices that still have a balance due."""
    qs = (
        Invoice.objects.filter(
            patient_id=patient_id,
            kind__in=_PENALTY_KINDS,
            status__in=_OPEN_INVOICE_STATUSES,
        )
        .select_related("appointment")
        .prefetch_related(
            Prefetch(
                "payments",
                queryset=Payment.objects.filter(status=Payment.Status.SUCCESSFUL),
            ),
        )
        .order_by("created_at")
    )
    if exclude_invoice_id:
        qs = qs.exclude(pk=exclude_invoice_id)
    return [inv for inv in qs if invoice_amount_due(inv) > Decimal("0")]


def patient_balance_due_by_id(patient_ids: list[int]) -> dict[int, str]:
    """Total still owed across all open invoices (respects partial cash payments)."""
    if not patient_ids:
        return {}
    out = {pid: Decimal("0") for pid in patient_ids}
    for inv in Invoice.objects.filter(
        patient_id__in=patient_ids,
        status__in=_OPEN_INVOICE_STATUSES,
    ).prefetch_related(
        Prefetch(
            "payments",
            queryset=Payment.objects.filter(status=Payment.Status.SUCCESSFUL),
        ),
    ):
        due = invoice_amount_due(inv)
        if due > Decimal("0"):
            out[inv.patient_id] += due
    return {pid: str(out[pid].quantize(Decimal("0.01"))) for pid in patient_ids}


def _money_str(d: Decimal) -> str:
    return str(d.quantize(Decimal("0.01")))


def _serialize_pending_invoice(inv: Invoice) -> dict:
    appt = inv.appointment
    due = invoice_amount_due(inv)
    return {
        "id": inv.id,
        "invoice_number": inv.invoice_number,
        "kind": inv.kind,
        "kind_label": _KIND_LABELS.get(inv.kind, inv.kind),
        "total_amount": _money_str(Decimal(inv.total_amount or 0)),
        "amount_due": _money_str(due),
        "appointment_date": str(appt.appointment_date) if appt else None,
    }


def build_doctor_pending_payment_context(
    patient_id: int,
    *,
    current_invoice_id: int | None = None,
) -> dict:
    """
    Context for doctor UI after completing a visit: prior penalty balances + collect today only or all.
    Amounts use remaining balance (e.g. $30 left on an $80 no-show after $50 cash).
    """
    open_invoices = list(_open_invoices_for_patient(patient_id))

    current_amount = Decimal("0")
    if current_invoice_id:
        cur = next((i for i in open_invoices if i.pk == current_invoice_id), None)
        if cur:
            current_amount = invoice_amount_due(cur)

    other_invoices = [
        inv
        for inv in open_invoices
        if inv.pk != current_invoice_id and inv.kind in _PENALTY_KINDS and invoice_amount_due(inv) > Decimal("0")
    ]
    other_total = sum((invoice_amount_due(i) for i in other_invoices), Decimal("0")).quantize(Decimal("0.01"))
    combined = (current_amount + other_total).quantize(Decimal("0.01"))

    balance_total = sum((invoice_amount_due(i) for i in open_invoices), Decimal("0")).quantize(Decimal("0.01"))
    balance_penalties = sum(
        (invoice_amount_due(i) for i in open_invoices if i.kind in _PENALTY_KINDS),
        Decimal("0"),
    ).quantize(Decimal("0.01"))

    return {
        "balance_total": _money_str(balance_total),
        "balance_penalties": _money_str(balance_penalties),
        "has_other_pending": other_total > Decimal("0"),
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
    ids.extend(i.pk for i in open_penalty_invoices_for_patient(primary_invoice.patient_id, exclude_invoice_id=primary_invoice.pk))
    return ids


def combined_total_for_invoice_ids(invoice_ids: list[int]) -> Decimal:
    """Sum remaining amount due on each invoice (partial payments respected)."""
    if not invoice_ids:
        return Decimal("0")
    total = Decimal("0")
    invoices = Invoice.objects.filter(pk__in=invoice_ids).prefetch_related(
        Prefetch(
            "payments",
            queryset=Payment.objects.filter(status=Payment.Status.SUCCESSFUL),
        ),
    )
    for inv in invoices:
        if inv.status in _OPEN_INVOICE_STATUSES:
            total += invoice_amount_due(inv)
    return total.quantize(Decimal("0.01"))
