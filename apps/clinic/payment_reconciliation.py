"""Staff payment reconciliation: fix open invoices that are already fully covered, and list mismatches."""

from __future__ import annotations

import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import Prefetch, Q
from django.utils import timezone

from apps.clinic.invoice_collection import (
    invoice_amount_due,
    invoice_cash_and_card_paid_total,
    set_appointment_status_after_invoice_paid,
)
from apps.clinic.models import Invoice, Payment

logger = logging.getLogger(__name__)

_OPEN = (Invoice.Status.ISSUED, Invoice.Status.OVERDUE, Invoice.Status.DRAFT)


def _money(v) -> str:
    return str(Decimal(v or 0).quantize(Decimal("0.01")))


def close_invoice_if_zero_due(invoice: Invoice) -> dict:
    """
    If an open invoice has amount due of $0 (payments already cover it), mark PAID.
    Does not create a new Payment row — only corrects status/appointment.
    """
    with transaction.atomic():
        inv = (
            Invoice.objects.select_for_update()
            .select_related("appointment", "patient")
            .prefetch_related(
                Prefetch(
                    "payments",
                    queryset=Payment.objects.filter(status=Payment.Status.SUCCESSFUL),
                )
            )
            .get(pk=invoice.pk)
        )
        if inv.status == Invoice.Status.PAID:
            return {
                "ok": True,
                "closed": False,
                "invoice_id": inv.id,
                "detail": "Invoice is already marked paid.",
            }
        if inv.status not in _OPEN:
            return {
                "ok": False,
                "closed": False,
                "invoice_id": inv.id,
                "detail": f"Invoice cannot be closed from status {inv.status}.",
            }
        due = invoice_amount_due(inv)
        if due > Decimal("0"):
            return {
                "ok": False,
                "closed": False,
                "invoice_id": inv.id,
                "amount_due": _money(due),
                "detail": f"Still owes ${_money(due)}. Record cash/card or Check Square first.",
            }
        paid_total = invoice_cash_and_card_paid_total(inv)
        inv.status = Invoice.Status.PAID
        inv.paid_at = timezone.now()
        inv.save(update_fields=["status", "paid_at", "updated_at"])
        set_appointment_status_after_invoice_paid(inv)
        logger.info(
            "Reconciliation closed invoice %s as paid (local payments $%s covered total $%s)",
            inv.pk,
            paid_total,
            inv.total_amount,
        )
        return {
            "ok": True,
            "closed": True,
            "invoice_id": inv.id,
            "invoice_number": inv.invoice_number,
            "detail": f"Marked {inv.invoice_number} paid — local payments already covered the balance.",
        }


def close_all_zero_due_open_invoices(*, limit: int = 200) -> dict:
    """Batch-close open invoices with $0 still due. Safe; skips anything still owing."""
    closed = 0
    skipped = 0
    details: list[str] = []
    qs = (
        Invoice.objects.filter(status__in=_OPEN)
        .select_related("patient", "appointment")
        .order_by("-issued_at", "-id")[: max(1, min(limit, 500))]
    )
    for inv in qs:
        due = invoice_amount_due(inv)
        if due > Decimal("0"):
            continue
        out = close_invoice_if_zero_due(inv)
        if out.get("closed"):
            closed += 1
            details.append(out.get("detail") or inv.invoice_number)
        else:
            skipped += 1
    return {
        "ok": True,
        "closed_count": closed,
        "skipped_count": skipped,
        "detail": (
            f"Closed {closed} fully paid invoice(s) that were still marked open."
            if closed
            else "No fully paid open invoices needed closing."
        ),
        "closed_details": details[:40],
    }


def _payment_summary(invoice: Invoice) -> list[dict]:
    rows = []
    for p in invoice.payments.filter(status=Payment.Status.SUCCESSFUL).order_by("paid_at", "id"):
        rows.append(
            {
                "id": p.id,
                "amount": _money(p.amount),
                "payment_method": p.payment_method,
                "payment_reference": p.payment_reference or "",
                "paid_at": p.paid_at.isoformat() if p.paid_at else None,
            }
        )
    return rows


def build_payment_reconciliation_payload(*, q: str = "", page: int = 1, page_size: int = 30) -> dict:
    """
    Lists open invoices that need attention for desk/Square reconciliation.
    Primary focus: fully covered locally but still open (cash recorded, status not flipped).
    """
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 30), 100))
    qs = (
        Invoice.objects.filter(status__in=_OPEN)
        .select_related("patient", "appointment")
        .prefetch_related(
            Prefetch(
                "payments",
                queryset=Payment.objects.filter(status=Payment.Status.SUCCESSFUL),
            )
        )
        .order_by("-issued_at", "-id")
    )
    query = (q or "").strip()
    if query:
        name_q = (
            Q(patient__first_name__icontains=query)
            | Q(patient__last_name__icontains=query)
            | Q(invoice_number__icontains=query)
        )
        if query.isdigit():
            name_q |= Q(patient_id=int(query)) | Q(pk=int(query))
        qs = qs.filter(name_q)

    zero_due_rows: list[dict] = []
    partial_rows: list[dict] = []
    open_unpaid_rows: list[dict] = []

    # Scan a bounded window so the page stays responsive.
    for inv in qs[:500]:
        due = invoice_amount_due(inv)
        paid = invoice_cash_and_card_paid_total(inv)
        patient_name = f"{inv.patient.first_name} {inv.patient.last_name}".strip()
        base = {
            "invoice_id": inv.id,
            "invoice_number": inv.invoice_number,
            "patient_id": inv.patient_id,
            "patient_name": patient_name,
            "status": inv.status,
            "kind": inv.kind,
            "total_amount": _money(inv.total_amount),
            "amount_paid": _money(paid),
            "amount_due": _money(due),
            "issued_at": inv.issued_at.isoformat() if inv.issued_at else None,
            "appointment_date": (
                str(inv.appointment.appointment_date) if inv.appointment_id else None
            ),
            "payments": _payment_summary(inv),
            "has_cash_payment": any(p.payment_method == Payment.Method.CASH for p in inv.payments.all()),
        }
        if due <= Decimal("0"):
            zero_due_rows.append({**base, "issue": "fully_paid_still_open"})
        elif paid > Decimal("0"):
            partial_rows.append({**base, "issue": "partial_payment"})
        else:
            open_unpaid_rows.append({**base, "issue": "open_unpaid"})

    def paginate(items: list[dict]) -> dict:
        total = len(items)
        start = (page - 1) * page_size
        chunk = items[start : start + page_size]
        return {
            "count": total,
            "page": page,
            "page_size": page_size,
            "results": chunk,
        }

    return {
        "summary": {
            "fully_paid_still_open": len(zero_due_rows),
            "partial_payment": len(partial_rows),
            "open_unpaid": len(open_unpaid_rows),
        },
        "fully_paid_still_open": paginate(zero_due_rows),
        "partial_payment": paginate(partial_rows),
        "open_unpaid": paginate(open_unpaid_rows),
        "hints": [
            "Fully paid but still open: cash/card was recorded, or a full professional discount brought the bill to $0, but the invoice stayed Issued — use Close as paid.",
            "Partial payment: some cash/card is on file; remaining balance is still due.",
            "Open unpaid: no local cash/card payment yet — use Check Square, Mark paid (if Square app shows paid), or Record cash on Billing.",
        ],
    }
