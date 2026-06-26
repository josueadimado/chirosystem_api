"""Admin removal of visits from patient chart history."""

from decimal import Decimal

import pytest

from apps.clinic.models import Appointment, Invoice
from apps.clinic.patient_history_admin import (
    admin_may_remove_appointment_from_patient_chart,
    remove_appointment_from_patient_chart,
)


@pytest.mark.django_db
def test_may_remove_no_show_with_unpaid_fee(appointment_factory, patient, provider, service):
    appt = appointment_factory(patient=patient, provider=provider, booked_service=service)
    appt.status = Appointment.Status.NO_SHOW
    appt.save(update_fields=["status", "updated_at"])
    Invoice.objects.create(
        patient=patient,
        appointment=appt,
        invoice_number="INV-NS-DEL-1",
        subtotal=Decimal("55.00"),
        tax=Decimal("0"),
        discount=Decimal("0"),
        total_amount=Decimal("55.00"),
        status=Invoice.Status.ISSUED,
        kind=Invoice.Kind.NO_SHOW_FEE,
    )
    ok, err = admin_may_remove_appointment_from_patient_chart(appt)
    assert ok is True
    assert err == ""


@pytest.mark.django_db
def test_cannot_remove_paid_visit(appointment_factory, patient, provider, service):
    appt = appointment_factory(patient=patient, provider=provider, booked_service=service)
    appt.status = Appointment.Status.COMPLETED
    appt.save(update_fields=["status", "updated_at"])
    Invoice.objects.create(
        patient=patient,
        appointment=appt,
        invoice_number="INV-PAID-1",
        subtotal=Decimal("1.00"),
        tax=Decimal("0"),
        discount=Decimal("0"),
        total_amount=Decimal("1.00"),
        status=Invoice.Status.PAID,
        kind=Invoice.Kind.VISIT,
    )
    ok, err = admin_may_remove_appointment_from_patient_chart(appt)
    assert ok is False
    assert "paid" in err.lower()


@pytest.mark.django_db
def test_remove_deletes_appointment_and_invoice(appointment_factory, patient, provider, service):
    appt = appointment_factory(patient=patient, provider=provider, booked_service=service)
    appt.status = Appointment.Status.NO_SHOW
    appt.save(update_fields=["status", "updated_at"])
    Invoice.objects.create(
        patient=patient,
        appointment=appt,
        invoice_number="INV-NS-DEL-2",
        subtotal=Decimal("55.00"),
        tax=Decimal("0"),
        discount=Decimal("0"),
        total_amount=Decimal("55.00"),
        status=Invoice.Status.ISSUED,
        kind=Invoice.Kind.NO_SHOW_FEE,
    )
    aid = appt.id
    remove_appointment_from_patient_chart(appt)
    assert not Appointment.objects.filter(pk=aid).exists()
    assert not Invoice.objects.filter(invoice_number="INV-NS-DEL-2").exists()
