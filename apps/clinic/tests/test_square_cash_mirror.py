"""Square cash mirror for clinic Record-cash payments."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apps.clinic.models import Appointment, Invoice, Payment
from apps.clinic.serializers import PaymentCompleteSerializer
from apps.clinic.square_payment import (
    _LOCAL_CASH_REF_PREFIX,
    _square_payment_is_recorded_cash,
    _square_payment_matches_invoice,
    record_local_cash_payment_in_square,
)


@pytest.mark.django_db
def test_cash_pay_schedules_square_mirror(appointment_factory, patient, provider, service):
    appt = appointment_factory(patient=patient, provider=provider, booked_service=service)
    appt.status = Appointment.Status.AWAITING_PAYMENT
    appt.save(update_fields=["status", "updated_at"])
    inv = Invoice.objects.create(
        patient=patient,
        appointment=appt,
        invoice_number="INV-CASH-MIRROR-1",
        subtotal=Decimal("40.00"),
        tax=Decimal("0"),
        discount=Decimal("0"),
        total_amount=Decimal("40.00"),
        status=Invoice.Status.ISSUED,
        kind=Invoice.Kind.VISIT,
    )

    with patch("apps.clinic.serializers.transaction.on_commit") as on_commit:
        ser = PaymentCompleteSerializer(
            data={"amount": "40.00", "payment_method": Payment.Method.CASH},
        )
        ser.is_valid(raise_exception=True)
        payment = ser.save(invoice=inv)

    assert payment.status == Payment.Status.SUCCESSFUL
    inv.refresh_from_db()
    assert inv.status == Invoice.Status.PAID
    assert on_commit.called
    # Run the scheduled callback with Square mocked.
    callback = on_commit.call_args[0][0]
    with patch("apps.clinic.square_payment.record_local_cash_payment_in_square") as mirror:
        callback()
        mirror.assert_called_once_with(payment.id)


@pytest.mark.django_db
def test_non_cash_pay_does_not_schedule_square_mirror(appointment_factory, patient, provider, service):
    appt = appointment_factory(patient=patient, provider=provider, booked_service=service)
    inv = Invoice.objects.create(
        patient=patient,
        appointment=appt,
        invoice_number="INV-CASH-MIRROR-2",
        subtotal=Decimal("40.00"),
        tax=Decimal("0"),
        discount=Decimal("0"),
        total_amount=Decimal("40.00"),
        status=Invoice.Status.ISSUED,
        kind=Invoice.Kind.VISIT,
    )

    with patch("apps.clinic.serializers.transaction.on_commit") as on_commit:
        ser = PaymentCompleteSerializer(
            data={"amount": "40.00", "payment_method": Payment.Method.MANUAL},
        )
        ser.is_valid(raise_exception=True)
        ser.save(invoice=inv)

    on_commit.assert_not_called()


@pytest.mark.django_db
def test_record_local_cash_creates_square_cash_payment(appointment_factory, patient, provider, service):
    appt = appointment_factory(patient=patient, provider=provider, booked_service=service)
    inv = Invoice.objects.create(
        patient=patient,
        appointment=appt,
        invoice_number="INV-CASH-MIRROR-3",
        subtotal=Decimal("25.00"),
        tax=Decimal("0"),
        discount=Decimal("0"),
        total_amount=Decimal("25.00"),
        status=Invoice.Status.ISSUED,
        kind=Invoice.Kind.VISIT,
    )
    payment = Payment.objects.create(
        invoice=inv,
        patient=patient,
        amount=Decimal("25.00"),
        payment_method=Payment.Method.CASH,
        status=Payment.Status.SUCCESSFUL,
    )

    mock_client = MagicMock()
    mock_client.payments.create.return_value = SimpleNamespace(
        errors=None,
        payment=SimpleNamespace(id="sqcash_abc123", status="COMPLETED"),
    )

    with (
        patch("apps.clinic.square_payment.square_configured", return_value=True),
        patch("apps.clinic.square_payment.get_location_id", return_value="LOC1"),
        patch("apps.clinic.square_payment.get_square_client", return_value=mock_client),
    ):
        out = record_local_cash_payment_in_square(payment.id)

    assert out["ok"] is True
    assert out["payment_id"] == "sqcash_abc123"
    kwargs = mock_client.payments.create.call_args.kwargs
    assert kwargs["source_id"] == "CASH"
    assert kwargs["reference_id"] == f"{_LOCAL_CASH_REF_PREFIX}{payment.id}"
    assert kwargs["idempotency_key"] == f"clinic-cash-{payment.id}"
    payment.refresh_from_db()
    assert payment.payment_reference == "sqcash_abc123"


@pytest.mark.django_db
def test_square_cash_failure_does_not_raise(appointment_factory, patient, provider, service):
    appt = appointment_factory(patient=patient, provider=provider, booked_service=service)
    inv = Invoice.objects.create(
        patient=patient,
        appointment=appt,
        invoice_number="INV-CASH-MIRROR-4",
        subtotal=Decimal("10.00"),
        tax=Decimal("0"),
        discount=Decimal("0"),
        total_amount=Decimal("10.00"),
        status=Invoice.Status.ISSUED,
        kind=Invoice.Kind.VISIT,
    )
    payment = Payment.objects.create(
        invoice=inv,
        patient=patient,
        amount=Decimal("10.00"),
        payment_method=Payment.Method.CASH,
        status=Payment.Status.SUCCESSFUL,
    )

    mock_client = MagicMock()
    mock_client.payments.create.side_effect = RuntimeError("network down")

    with (
        patch("apps.clinic.square_payment.square_configured", return_value=True),
        patch("apps.clinic.square_payment.get_location_id", return_value="LOC1"),
        patch("apps.clinic.square_payment.get_square_client", return_value=mock_client),
    ):
        out = record_local_cash_payment_in_square(payment.id)

    assert out["ok"] is False
    payment.refresh_from_db()
    assert payment.payment_reference == ""


def test_cash_payments_excluded_from_invoice_match():
    pay = SimpleNamespace(
        status="COMPLETED",
        source_type="CASH",
        reference_id=f"{_LOCAL_CASH_REF_PREFIX}99",
        note="Cash · Invoice INV-X",
        order_id=None,
    )
    inv = SimpleNamespace(pk=1, invoice_number="INV-X")
    assert _square_payment_is_recorded_cash(pay) is True
    assert _square_payment_matches_invoice(MagicMock(), pay, inv) is False
