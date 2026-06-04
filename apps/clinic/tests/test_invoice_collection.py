"""Collectible invoice rules for visit and penalty fees."""

from decimal import Decimal

import pytest

from apps.clinic.invoice_collection import open_invoice_for_appointment_payment
from apps.clinic.models import Appointment, Invoice


@pytest.mark.django_db
def test_no_show_with_unpaid_fee_invoice_is_collectible(appointment_factory, patient, provider, service):
    appt = appointment_factory(patient=patient, provider=provider, booked_service=service)
    appt.status = Appointment.Status.NO_SHOW
    appt.save(update_fields=["status", "updated_at"])
    Invoice.objects.create(
        patient=patient,
        appointment=appt,
        invoice_number="INV-NS-TEST-1",
        subtotal=Decimal("25.00"),
        tax=Decimal("0"),
        discount=Decimal("0"),
        total_amount=Decimal("25.00"),
        status=Invoice.Status.ISSUED,
        kind=Invoice.Kind.NO_SHOW_FEE,
    )
    inv = open_invoice_for_appointment_payment(appt)
    assert inv is not None
    assert inv.kind == Invoice.Kind.NO_SHOW_FEE


@pytest.mark.django_db
def test_no_show_without_invoice_not_collectible(appointment_factory, patient, provider, service):
    appt = appointment_factory(patient=patient, provider=provider, booked_service=service)
    appt.status = Appointment.Status.NO_SHOW
    appt.save(update_fields=["status", "updated_at"])
    assert open_invoice_for_appointment_payment(appt) is None


@pytest.mark.django_db
def test_awaiting_payment_visit_invoice_collectible(appointment_factory, patient, provider, service):
    appt = appointment_factory(patient=patient, provider=provider, booked_service=service)
    appt.status = Appointment.Status.AWAITING_PAYMENT
    appt.save(update_fields=["status", "updated_at"])
    Invoice.objects.create(
        patient=patient,
        appointment=appt,
        invoice_number="INV-VISIT-1",
        subtotal=Decimal("55.00"),
        tax=Decimal("0"),
        discount=Decimal("0"),
        total_amount=Decimal("55.00"),
        status=Invoice.Status.ISSUED,
        kind=Invoice.Kind.VISIT,
    )
    inv = open_invoice_for_appointment_payment(appt)
    assert inv is not None
    assert inv.kind == Invoice.Kind.VISIT
