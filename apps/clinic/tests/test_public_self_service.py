"""Patient self-service cancel/reschedule — upcoming appointment lookup."""

from datetime import date, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.clinic.models import Appointment, Patient, Provider, Service
from apps.clinic.public_booking_service import (
    appointment_to_self_service_payload,
    cancel_appointment_public,
    normalize_caller_phone,
    public_self_service_household_context,
    public_self_service_upcoming_appointments,
)


class PublicSelfServiceTests(TestCase):
    def setUp(self):
        self.phone = "+15555550123"
        self.patient = Patient.objects.create(
            first_name="Self",
            last_name="Service",
            phone=self.phone,
        )
        self.service = Service.objects.create(
            name="Adjustment",
            duration_minutes=30,
            price=Decimal("55.00"),
            is_active=True,
            show_in_public_booking=True,
            service_type="chiropractic",
        )
        user = get_user_model().objects.create_user(
            username="dr_self",
            password="Doctor123!",
            role="doctor",
            full_name="Dr Self",
        )
        self.provider = Provider.objects.create(user=user, title="DC", specialty="Chiropractic", active=True)
        self.provider.services.add(self.service)
        self.today = date.today()
        self.future = self.today + timedelta(days=3)

    def test_normalize_caller_phone_e164(self):
        self.assertEqual(normalize_caller_phone("+15555550123"), self.phone)

    @patch("apps.clinic.clinic_time.clinic_localdate")
    def test_upcoming_includes_future_booked(self, mock_today):
        mock_today.return_value = self.today
        appt = Appointment.objects.create(
            patient=self.patient,
            provider=self.provider,
            booked_service=self.service,
            appointment_date=self.future,
            start_time=time(10, 0),
            end_time=time(10, 30),
            status=Appointment.Status.BOOKED,
        )
        rows, hint = public_self_service_upcoming_appointments(self.phone)
        self.assertIsNone(hint)
        self.assertEqual([a.id for a in rows], [appt.id])

    @patch("apps.clinic.clinic_time.clinic_localdate")
    @patch("apps.clinic.public_booking_service._local_now_passed_appointment_start", return_value=True)
    def test_upcoming_skips_today_after_start(self, _passed, mock_today):
        mock_today.return_value = self.today
        Appointment.objects.create(
            patient=self.patient,
            provider=self.provider,
            booked_service=self.service,
            appointment_date=self.today,
            start_time=time(9, 0),
            end_time=time(9, 30),
            status=Appointment.Status.BOOKED,
        )
        rows, hint = public_self_service_upcoming_appointments(self.phone)
        self.assertEqual(rows, [])
        self.assertIn("already started or passed", hint or "")

    @patch("apps.clinic.clinic_time.clinic_localdate")
    def test_cancel_future_appointment(self, mock_today):
        mock_today.return_value = self.today
        appt = Appointment.objects.create(
            patient=self.patient,
            provider=self.provider,
            booked_service=self.service,
            appointment_date=self.future,
            start_time=time(14, 0),
            end_time=time(14, 30),
            status=Appointment.Status.BOOKED,
        )
        cancelled, err = cancel_appointment_public(
            phone_normalized=self.phone,
            appointment_id=appt.id,
        )
        self.assertIsNone(err)
        self.assertIsNotNone(cancelled)
        appt.refresh_from_db()
        self.assertEqual(appt.status, Appointment.Status.CANCELLED)

    @patch("apps.clinic.clinic_time.clinic_localdate")
    def test_cancel_rejects_wrong_phone(self, mock_today):
        mock_today.return_value = self.today
        appt = Appointment.objects.create(
            patient=self.patient,
            provider=self.provider,
            booked_service=self.service,
            appointment_date=self.future,
            start_time=time(14, 0),
            end_time=time(14, 30),
            status=Appointment.Status.BOOKED,
        )
        _, err = cancel_appointment_public(
            phone_normalized="+15555550999",
            appointment_id=appt.id,
        )
        self.assertIsNotNone(err)
        self.assertIn("does not match", err.lower())

    @patch("apps.clinic.clinic_time.clinic_localdate")
    def test_household_phone_returns_both_patients_appointments(self, mock_today):
        mock_today.return_value = self.today
        parent = Patient.objects.create(
            first_name="Maria",
            last_name="Lopez",
            phone=self.phone,
        )
        child = Patient.objects.create(
            first_name="Juan",
            last_name="Lopez",
            phone=self.phone,
        )
        same_time = time(14, 0)
        appt_parent = Appointment.objects.create(
            patient=parent,
            provider=self.provider,
            booked_service=self.service,
            appointment_date=self.future,
            start_time=same_time,
            end_time=time(14, 30),
            status=Appointment.Status.BOOKED,
        )
        massage = Service.objects.create(
            name="Massage",
            duration_minutes=60,
            price=Decimal("80.00"),
            is_active=True,
            show_in_public_booking=True,
            service_type="massage",
        )
        self.provider.services.add(massage)
        appt_child = Appointment.objects.create(
            patient=child,
            provider=self.provider,
            booked_service=massage,
            appointment_date=self.future,
            start_time=same_time,
            end_time=time(15, 0),
            status=Appointment.Status.BOOKED,
        )
        rows, hint = public_self_service_upcoming_appointments(self.phone)
        self.assertIsNone(hint)
        self.assertEqual({a.id for a in rows}, {appt_parent.id, appt_child.id})

        household = public_self_service_household_context(self.phone)
        self.assertTrue(household["ambiguous_phone"])
        self.assertEqual(len(household["household_members"]), 2)

        payloads = [appointment_to_self_service_payload(a) for a in rows]
        names = {p["patient_name"] for p in payloads}
        self.assertEqual(names, {"Maria Lopez", "Juan Lopez"})
        self.assertTrue(all(p["patient_name"] for p in payloads))
