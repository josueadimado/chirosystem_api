"""Payment reconciliation: close open invoices that are already fully paid locally."""

from decimal import Decimal

import pytest

from apps.clinic.models import Appointment, Invoice, Payment
from apps.clinic.payment_reconciliation import (
    build_payment_reconciliation_payload,
    close_all_zero_due_open_invoices,
    close_invoice_if_zero_due,
)


@pytest.mark.django_db
def test_close_zero_due_marks_paid(appointment_factory, patient, provider, service):
    appt = appointment_factory(patient=patient, provider=provider, booked_service=service)
    appt.status = Appointment.Status.AWAITING_PAYMENT
    appt.save(update_fields=["status", "updated_at"])
    inv = Invoice.objects.create(
        patient=patient,
        appointment=appt,
        invoice_number="INV-RECON-1",
        subtotal=Decimal("50.00"),
        tax=Decimal("0"),
        discount=Decimal("0"),
        total_amount=Decimal("50.00"),
        status=Invoice.Status.ISSUED,
        kind=Invoice.Kind.VISIT,
    )
    Payment.objects.create(
        invoice=inv,
        patient=patient,
        amount=Decimal("50.00"),
        payment_method=Payment.Method.CASH,
        status=Payment.Status.SUCCESSFUL,
    )
    out = close_invoice_if_zero_due(inv)
    assert out["ok"] is True
    assert out["closed"] is True
    inv.refresh_from_db()
    assert inv.status == Invoice.Status.PAID


@pytest.mark.django_db
def test_close_zero_due_full_professional_discount(appointment_factory, patient, provider, service):
    """$0 total after full discount — still Issued / awaiting payment until closed."""
    appt = appointment_factory(patient=patient, provider=provider, booked_service=service)
    appt.status = Appointment.Status.AWAITING_PAYMENT
    appt.save(update_fields=["status", "updated_at"])
    inv = Invoice.objects.create(
        patient=patient,
        appointment=appt,
        invoice_number="INV-RECON-DISC",
        subtotal=Decimal("55.00"),
        tax=Decimal("0"),
        discount=Decimal("55.00"),
        total_amount=Decimal("0.00"),
        status=Invoice.Status.ISSUED,
        kind=Invoice.Kind.VISIT,
    )
    out = close_invoice_if_zero_due(inv)
    assert out["ok"] is True
    assert out["closed"] is True
    inv.refresh_from_db()
    appt.refresh_from_db()
    assert inv.status == Invoice.Status.PAID
    assert appt.status == Appointment.Status.COMPLETED


@pytest.mark.django_db
def test_close_zero_due_refuses_when_balance_remains(appointment_factory, patient, provider, service):
    appt = appointment_factory(patient=patient, provider=provider, booked_service=service)
    inv = Invoice.objects.create(
        patient=patient,
        appointment=appt,
        invoice_number="INV-RECON-2",
        subtotal=Decimal("80.00"),
        tax=Decimal("0"),
        discount=Decimal("0"),
        total_amount=Decimal("80.00"),
        status=Invoice.Status.ISSUED,
        kind=Invoice.Kind.VISIT,
    )
    Payment.objects.create(
        invoice=inv,
        patient=patient,
        amount=Decimal("30.00"),
        payment_method=Payment.Method.CASH,
        status=Payment.Status.SUCCESSFUL,
    )
    out = close_invoice_if_zero_due(inv)
    assert out["ok"] is False
    assert out["closed"] is False
    inv.refresh_from_db()
    assert inv.status == Invoice.Status.ISSUED


@pytest.mark.django_db
def test_reconciliation_lists_fully_paid_open(appointment_factory, patient, provider, service):
    appt = appointment_factory(patient=patient, provider=provider, booked_service=service)
    inv = Invoice.objects.create(
        patient=patient,
        appointment=appt,
        invoice_number="INV-RECON-3",
        subtotal=Decimal("40.00"),
        tax=Decimal("0"),
        discount=Decimal("0"),
        total_amount=Decimal("40.00"),
        status=Invoice.Status.ISSUED,
        kind=Invoice.Kind.VISIT,
    )
    Payment.objects.create(
        invoice=inv,
        patient=patient,
        amount=Decimal("40.00"),
        payment_method=Payment.Method.CASH,
        status=Payment.Status.SUCCESSFUL,
    )
    payload = build_payment_reconciliation_payload()
    assert payload["summary"]["fully_paid_still_open"] >= 1
    ids = [r["invoice_id"] for r in payload["fully_paid_still_open"]["results"]]
    assert inv.id in ids
    batch = close_all_zero_due_open_invoices()
    assert batch["closed_count"] >= 1
    inv.refresh_from_db()
    assert inv.status == Invoice.Status.PAID
