"""Public booking availability — slot generation for voice and web."""

from datetime import date, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.clinic.booking_availability import (
    public_available_slot_times_for_provider,
    public_available_slot_times_for_service,
)
from apps.clinic.models import Appointment, Patient, Provider, Service


class BookingAvailabilityTests(TestCase):
    def setUp(self):
        self.monday = date(2026, 6, 8)  # Monday
        self.service = Service.objects.create(
            name="Adjustment",
            duration_minutes=30,
            price=Decimal("55.00"),
            is_active=True,
            show_in_public_booking=True,
            service_type="chiropractic",
        )
        user_a = get_user_model().objects.create_user(
            username="dr_a",
            password="Doctor123!",
            role="doctor",
            full_name="Dr A",
        )
        user_b = get_user_model().objects.create_user(
            username="dr_b",
            password="Doctor123!",
            role="doctor",
            full_name="Dr B",
        )
        self.provider_a = Provider.objects.create(user=user_a, title="DC", specialty="Chiropractic", active=True)
        self.provider_b = Provider.objects.create(user=user_b, title="DC", specialty="Chiropractic", active=True)
        self.provider_a.services.add(self.service)
        self.provider_b.services.add(self.service)
        self.patient = Patient.objects.create(first_name="Pat", last_name="Test", phone="+15555550199")

    def _book_provider(self, provider, start: time):
        end_h, end_m = divmod(start.hour * 60 + start.minute + 30, 60)
        Appointment.objects.create(
            patient=self.patient,
            provider=provider,
            booked_service=self.service,
            appointment_date=self.monday,
            start_time=start,
            end_time=time(end_h, end_m),
            status=Appointment.Status.BOOKED,
        )

    @patch("apps.clinic.online_booking_hours._clinic_minutes_for_date", return_value=(8 * 60, 18 * 60))
    def test_service_union_when_first_provider_full(self, _clinic_hours):
        """Voice AI should see slots on provider B when provider A is booked at 10:00."""
        self._book_provider(self.provider_a, time(10, 0))

        only_a = public_available_slot_times_for_provider(
            provider=self.provider_a,
            service=self.service,
            appt_date=self.monday,
        )
        self.assertNotIn(time(10, 0), only_a)

        only_b = public_available_slot_times_for_provider(
            provider=self.provider_b,
            service=self.service,
            appt_date=self.monday,
        )
        self.assertIn(time(10, 0), only_b)

        merged = public_available_slot_times_for_service(
            service_id=self.service.id,
            appt_date=self.monday,
            provider_id=None,
        )
        self.assertIn(time(10, 0), merged)

    @patch("apps.clinic.online_booking_hours._clinic_minutes_for_date", return_value=(8 * 60, 18 * 60))
    def test_specific_provider_falls_back_in_service_query(self, _clinic_hours):
        self._book_provider(self.provider_a, time(9, 0))
        slots = public_available_slot_times_for_service(
            service_id=self.service.id,
            appt_date=self.monday,
            provider_id=self.provider_a.id,
        )
        self.assertNotIn(time(9, 0), slots)
        self.assertIn(time(9, 0), public_available_slot_times_for_service(
            service_id=self.service.id,
            appt_date=self.monday,
            provider_id=self.provider_b.id,
        ))
