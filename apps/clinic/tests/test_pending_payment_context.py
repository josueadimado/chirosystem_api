"""Doctor can collect today's visit only or visit + remaining penalty balances."""

from decimal import Decimal

import pytest

from apps.clinic.models import Appointment, Invoice, Payment
from apps.clinic.patient_payment_pending import build_doctor_pending_payment_context, combined_total_for_invoice_ids
from apps.clinic.serializers import PaymentCompleteSerializer


@pytest.mark.django_db
def test_pending_context_uses_remaining_penalty_balance(appointment_factory, patient, provider, service):
    old = appointment_factory(patient=patient, provider=provider, booked_service=service)
    old.status = Appointment.Status.NO_SHOW
    old.save(update_fields=["status", "updated_at"])
    ns_inv = Invoice.objects.create(
        patient=patient,
        appointment=old,
        invoice_number="INV-NS-OLD",
        subtotal=Decimal("80.00"),
        tax=Decimal("0"),
        discount=Decimal("0"),
        total_amount=Decimal("80.00"),
        status=Invoice.Status.ISSUED,
        kind=Invoice.Kind.NO_SHOW_FEE,
    )
    ser = PaymentCompleteSerializer(data={"amount": "50.00", "payment_method": Payment.Method.CASH})
    ser.is_valid(raise_exception=True)
    ser.save(invoice=ns_inv)

    today = appointment_factory(patient=patient, provider=provider, booked_service=service)
    today.status = Appointment.Status.AWAITING_PAYMENT
    today.save(update_fields=["status", "updated_at"])
    visit_inv = Invoice.objects.create(
        patient=patient,
        appointment=today,
        invoice_number="INV-VISIT-TODAY",
        subtotal=Decimal("55.00"),
        tax=Decimal("0"),
        discount=Decimal("0"),
        total_amount=Decimal("55.00"),
        status=Invoice.Status.ISSUED,
        kind=Invoice.Kind.VISIT,
    )

    ctx = build_doctor_pending_payment_context(patient.id, current_invoice_id=visit_inv.id)
    assert ctx["has_other_pending"] is True
    assert ctx["current_amount"] == "55.00"
    assert ctx["other_total"] == "30.00"
    assert ctx["combined_amount"] == "85.00"

    bundle = combined_total_for_invoice_ids(
        [visit_inv.id, ns_inv.id],
    )
    assert bundle == Decimal("85.00")
