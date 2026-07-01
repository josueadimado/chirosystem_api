"""Missed visit recovery — past check-in and reopen mistaken no-show."""

from datetime import date, time, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.clinic.missed_visit_recovery import (
    appointment_is_missed_no_show,
    reopen_missed_visit_to_booked,
    staff_may_checkin_appointment_date,
    staff_may_reopen_missed_visit,
)
from apps.clinic.models import Appointment, Invoice, Patient, Provider, Service, Visit
from apps.clinic.views import KioskViewSet


class MissedVisitRecoveryTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_user(
            username="desk_staff",
            password="Staff123!",
            role="staff",
            full_name="Desk Staff",
        )
        self.patient = Patient.objects.create(
            first_name="Pat",
            last_name="Late",
            phone="+15555550101",
        )
        self.service = Service.objects.create(
            name="Adjustment",
            duration_minutes=30,
            price=Decimal("55.00"),
            is_active=True,
            show_in_public_booking=True,
        )
        user = get_user_model().objects.create_user(
            username="dr_test",
            password="Doctor123!",
            role="doctor",
            full_name="Dr Test",
        )
        self.provider = Provider.objects.create(user=user, title="DC", specialty="Chiropractic", active=True)
        self.provider.services.add(self.service)
        yesterday = date.today() - timedelta(days=1)
        self.appt = Appointment.objects.create(
            patient=self.patient,
            provider=self.provider,
            booked_service=self.service,
            appointment_date=yesterday,
            start_time=time(10, 0),
            end_time=time(10, 30),
            status=Appointment.Status.BOOKED,
        )

    def test_staff_may_checkin_past_date(self):
        today = date.today()
        ok, _ = staff_may_checkin_appointment_date(self.appt.appointment_date, today=today)
        self.assertTrue(ok)

    def test_staff_may_not_checkin_future_date(self):
        future = date.today() + timedelta(days=2)
        ok, err = staff_may_checkin_appointment_date(future, today=date.today())
        self.assertFalse(ok)
        self.assertIn("future", err.lower())

    @patch("apps.notifications.tasks.notify_provider_patient_checked_in_task")
    def test_desk_checkin_past_booked_appointment(self, _notify):
        factory = APIRequestFactory()
        request = factory.post("/api/v1/kiosk/checkin/", {"appointment_id": self.appt.id}, format="json")
        force_authenticate(request, user=self.staff)
        response = KioskViewSet.as_view({"post": "checkin"})(request)
        self.assertEqual(response.status_code, 200, response.data)
        self.appt.refresh_from_db()
        self.assertEqual(self.appt.status, Appointment.Status.CHECKED_IN)
        self.assertIsNotNone(self.appt.checked_in_at)

    def test_reopen_unpaid_no_show_then_checkin(self):
        self.appt.status = Appointment.Status.NO_SHOW
        self.appt.auto_no_show_processed_at = timezone.now()
        self.appt.save(update_fields=["status", "auto_no_show_processed_at", "updated_at"])
        visit = Visit.objects.create(
            appointment=self.appt,
            patient=self.patient,
            provider=self.provider,
            status=Visit.Status.COMPLETED,
            doctor_notes="No-show fee (patient missed scheduled appointment).",
            completed_at=timezone.now(),
        )
        Invoice.objects.create(
            patient=self.patient,
            appointment=self.appt,
            visit=visit,
            invoice_number="INV-NS-TEST-1",
            subtotal=Decimal("55.00"),
            tax=Decimal("0"),
            discount=Decimal("0"),
            total_amount=Decimal("55.00"),
            status=Invoice.Status.ISSUED,
            kind=Invoice.Kind.NO_SHOW_FEE,
        )
        ok, _ = staff_may_reopen_missed_visit(self.appt)
        self.assertTrue(ok)
        reopen_missed_visit_to_booked(self.appt)
        self.appt.refresh_from_db()
        self.assertEqual(self.appt.status, Appointment.Status.BOOKED)
        self.assertIsNone(self.appt.auto_no_show_processed_at)
        inv = Invoice.objects.get(appointment=self.appt)
        self.assertEqual(inv.status, Invoice.Status.VOID)
        visit.refresh_from_db()
        self.assertEqual(visit.status, Visit.Status.OPEN)
        self.assertEqual(visit.doctor_notes, "")

    def test_cannot_reopen_paid_no_show(self):
        self.appt.status = Appointment.Status.NO_SHOW
        self.appt.save(update_fields=["status", "updated_at"])
        visit = Visit.objects.create(
            appointment=self.appt,
            patient=self.patient,
            provider=self.provider,
            status=Visit.Status.COMPLETED,
        )
        Invoice.objects.create(
            patient=self.patient,
            appointment=self.appt,
            visit=visit,
            invoice_number="INV-NS-PAID-1",
            subtotal=Decimal("55.00"),
            tax=Decimal("0"),
            discount=Decimal("0"),
            total_amount=Decimal("55.00"),
            status=Invoice.Status.PAID,
            kind=Invoice.Kind.NO_SHOW_FEE,
        )
        ok, err = staff_may_reopen_missed_visit(self.appt)
        self.assertFalse(ok)
        self.assertIn("paid", err.lower())
        self.assertTrue(appointment_is_missed_no_show(self.appt))
