"""Integration tests: kiosk check-in must persist checked_in in the database."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.clinic.models import Appointment, Patient, Provider, Service
from apps.clinic.timezone_utils import now_clinic, today_clinic

User = get_user_model()


def _make_provider(username: str, specialty: str = "chiropractic") -> Provider:
    user = User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password="test-pass-123",
        role="doctor",
    )
    return Provider.objects.create(
        user=user,
        specialty=specialty,
        primary_service_type=specialty if specialty in ("chiropractic", "massage") else "chiropractic",
    )


def _make_service(name: str, service_type: str = "chiropractic") -> Service:
    return Service.objects.create(
        name=name,
        duration_minutes=30,
        price="55.00",
        service_type=service_type,
        is_active=True,
        show_in_public_booking=True,
    )


def _make_appointment(
    *,
    patient: Patient,
    provider: Provider,
    service: Service,
    start: time,
) -> Appointment:
    start_dt = datetime.combine(today_clinic(), start)
    end_time = (start_dt + timedelta(minutes=30)).time()
    return Appointment.objects.create(
        patient=patient,
        provider=provider,
        booked_service=service,
        appointment_date=today_clinic(),
        start_time=start,
        end_time=end_time,
        status=Appointment.Status.BOOKED,
    )


@override_settings(KIOSK_EARLY_CHECKIN_MINUTES_BEFORE=120)
class KioskCheckinIntegrationTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.patient = Patient.objects.create(
            first_name="Giovanni",
            last_name="Leonor",
            phone="+12695550123",
            email="giovanni@example.com",
        )
        self.chiro_provider = _make_provider("dr_checkin_test")
        self.massage_provider = _make_provider("lmt_checkin_test", specialty="massage")
        self.chiro_svc = _make_service("Chiropractic Visit", "chiropractic")
        self.massage_svc = _make_service("Massage", "massage")

        clinic_now = now_clinic()
        soon = (clinic_now + timedelta(minutes=45)).time().replace(second=0, microsecond=0)
        later = (clinic_now + timedelta(hours=3)).time().replace(second=0, microsecond=0)

        self.chiro_appt = _make_appointment(
            patient=self.patient,
            provider=self.chiro_provider,
            service=self.chiro_svc,
            start=soon,
        )
        self.massage_appt = _make_appointment(
            patient=self.patient,
            provider=self.massage_provider,
            service=self.massage_svc,
            start=later,
        )

    @patch("apps.notifications.tasks.notify_provider_patient_checked_in_task.delay")
    def test_kiosk_checkin_persists_single_appointment(self, _notify):
        res = self.client.post(
            "/api/v1/kiosk/checkin/",
            {
                "appointment_id": self.chiro_appt.id,
                "appointment_ids": [self.chiro_appt.id],
                "phone": self.patient.phone,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual(body["status"], Appointment.Status.CHECKED_IN)
        self.assertEqual(body["checked_in_count"], 1)
        self.assertIn(self.chiro_appt.id, body["appointment_ids"])

        self.chiro_appt.refresh_from_db()
        self.massage_appt.refresh_from_db()
        self.assertEqual(self.chiro_appt.status, Appointment.Status.CHECKED_IN)
        self.assertIsNotNone(self.chiro_appt.checked_in_at)
        self.assertEqual(self.massage_appt.status, Appointment.Status.BOOKED)
        self.assertIsNone(self.massage_appt.checked_in_at)

    @patch("apps.notifications.tasks.notify_provider_patient_checked_in_task.delay")
    def test_second_appointment_checkin_after_first(self, _notify):
        self.client.post(
            "/api/v1/kiosk/checkin/",
            {
                "appointment_id": self.chiro_appt.id,
                "appointment_ids": [self.chiro_appt.id],
                "phone": self.patient.phone,
            },
            format="json",
        )
        res = self.client.post(
            "/api/v1/kiosk/checkin/",
            {
                "appointment_id": self.massage_appt.id,
                "appointment_ids": [self.massage_appt.id],
                "phone": self.patient.phone,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)

        self.massage_appt.refresh_from_db()
        self.assertEqual(self.massage_appt.status, Appointment.Status.CHECKED_IN)
        self.assertIsNotNone(self.massage_appt.checked_in_at)

    @patch("apps.notifications.tasks.notify_provider_patient_checked_in_task.delay")
    def test_rejects_batch_checkin_multiple_ids(self, _notify):
        res = self.client.post(
            "/api/v1/kiosk/checkin/",
            {
                "appointment_id": self.chiro_appt.id,
                "appointment_ids": [self.chiro_appt.id, self.massage_appt.id],
                "phone": self.patient.phone,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 400, res.content)
        self.chiro_appt.refresh_from_db()
        self.massage_appt.refresh_from_db()
        self.assertEqual(self.chiro_appt.status, Appointment.Status.BOOKED)
        self.assertEqual(self.massage_appt.status, Appointment.Status.BOOKED)

    @patch("apps.notifications.tasks.notify_provider_patient_checked_in_task.delay")
    def test_lookup_lists_separate_appointments(self, _notify):
        res = self.client.post(
            "/api/v1/kiosk/lookup/",
            {"phone": self.patient.phone},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual(body["result"], "choose_appointment")
        self.assertEqual(len(body["choices"]), 2)
        ids = {c["appointment_id"] for c in body["choices"]}
        self.assertEqual(ids, {self.chiro_appt.id, self.massage_appt.id})

    @patch("apps.notifications.tasks.notify_provider_patient_checked_in_task.delay")
    def test_desk_checkin_without_phone_persists(self, _notify):
        staff = User.objects.create_user(
            username="staff_checkin",
            email="staff@example.com",
            password="test-pass-123",
            role="staff",
        )
        self.client.force_authenticate(user=staff)
        res = self.client.post(
            "/api/v1/kiosk/checkin/",
            {"appointment_id": self.chiro_appt.id},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.chiro_appt.refresh_from_db()
        self.assertEqual(self.chiro_appt.status, Appointment.Status.CHECKED_IN)

    @patch("apps.notifications.tasks.notify_provider_patient_checked_in_task.delay")
    def test_already_checked_in_returns_error_not_fake_success(self, _notify):
        self.chiro_appt.status = Appointment.Status.CHECKED_IN
        self.chiro_appt.save(update_fields=["status", "updated_at"])
        res = self.client.post(
            "/api/v1/kiosk/checkin/",
            {
                "appointment_id": self.chiro_appt.id,
                "appointment_ids": [self.chiro_appt.id],
                "phone": self.patient.phone,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 400, res.content)
