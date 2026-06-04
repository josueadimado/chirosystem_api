"""Partial cash payments keep invoice open until fully paid."""

from decimal import Decimal

import pytest

from apps.clinic.invoice_collection import invoice_amount_due, invoice_payment_summary
from apps.clinic.models import Appointment, Invoice, Payment
from apps.clinic.serializers import PaymentCompleteSerializer


@pytest.mark.django_db
def test_partial_cash_leaves_balance(appointment_factory, patient, provider, service):
    appt = appointment_factory(patient=patient, provider=provider, booked_service=service)
    appt.status = Appointment.Status.NO_SHOW
    appt.save(update_fields=["status", "updated_at"])
    inv = Invoice.objects.create(
        patient=patient,
        appointment=appt,
        invoice_number="INV-NS-PARTIAL-1",
        subtotal=Decimal("80.00"),
        tax=Decimal("0"),
        discount=Decimal("0"),
        total_amount=Decimal("80.00"),
        status=Invoice.Status.ISSUED,
        kind=Invoice.Kind.NO_SHOW_FEE,
    )
    ser = PaymentCompleteSerializer(
        data={"amount": "50.00", "payment_method": Payment.Method.CASH, "payment_reference": ""},
    )
    ser.is_valid(raise_exception=True)
    ser.save(invoice=inv)

    inv.refresh_from_db()
    assert inv.status == Invoice.Status.ISSUED
    assert invoice_amount_due(inv) == Decimal("30.00")
    summary = invoice_payment_summary(inv)
    assert summary["amount_paid"] == "50.00"
    assert summary["amount_due"] == "30.00"


@pytest.mark.django_db
def test_full_cash_closes_invoice(appointment_factory, patient, provider, service):
    appt = appointment_factory(patient=patient, provider=provider, booked_service=service)
    appt.status = Appointment.Status.NO_SHOW
    appt.save(update_fields=["status", "updated_at"])
    inv = Invoice.objects.create(
        patient=patient,
        appointment=appt,
        invoice_number="INV-NS-PARTIAL-2",
        subtotal=Decimal("80.00"),
        tax=Decimal("0"),
        discount=Decimal("0"),
        total_amount=Decimal("80.00"),
        status=Invoice.Status.ISSUED,
        kind=Invoice.Kind.NO_SHOW_FEE,
    )
    PaymentCompleteSerializer(
        data={"amount": "50.00", "payment_method": Payment.Method.CASH},
    ).is_valid(raise_exception=True).save(invoice=inv)
    PaymentCompleteSerializer(
        data={"amount": "30.00", "payment_method": Payment.Method.CASH},
    ).is_valid(raise_exception=True).save(invoice=inv)

    inv.refresh_from_db()
    assert inv.status == Invoice.Status.PAID
    assert invoice_amount_due(inv) == Decimal("0.00")
