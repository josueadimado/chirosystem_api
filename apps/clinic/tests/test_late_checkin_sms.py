"""Late check-in SMS — text patients who have not checked in after start + delay."""

from datetime import date, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.clinic.late_checkin_sms import (
    appointment_past_late_checkin_threshold,
    process_late_checkin_sms,
)
from apps.clinic.models import Appointment, Patient, Provider, Service


@override_settings(
    LATE_CHECKIN_SMS_ENABLED=True,
    LATE_CHECKIN_SMS_MINUTES_AFTER_START=10,
    TWILIO_ACCOUNT_SID="ACtest",
    TWILIO_AUTH_TOKEN="testtoken",
    TWILIO_MESSAGING_SERVICE_SID="MGtest",
)
class LateCheckinSmsTests(TestCase):
    def setUp(self):
        self.patient = Patient.objects.create(
            first_name="Alex",
            last_name="Late",
            phone="+15555550101",
            sms_consent=True,
            notify_reminders="sms",
        )
        self.service = Service.objects.create(
            name="Adjustment",
            duration_minutes=30,
            price=Decimal("55.00"),
            is_active=True,
            show_in_public_booking=True,
        )
        user = get_user_model().objects.create_user(
            username="dr_late",
            password="Doctor123!",
            role="doctor",
            full_name="Dr Late",
        )
        self.provider = Provider.objects.create(user=user, title="DC", specialty="Chiropractic", active=True)
        self.provider.services.add(self.service)
        self.today = date.today()
        self.appt = Appointment.objects.create(
            patient=self.patient,
            provider=self.provider,
            booked_service=self.service,
            appointment_date=self.today,
            start_time=time(10, 0),
            end_time=time(10, 30),
            status=Appointment.Status.BOOKED,
        )

    @patch("apps.clinic.late_checkin_sms.clinic_now")
    def test_sends_sms_when_booked_and_past_threshold(self, mock_now):
        mock_now.return_value = timezone.make_aware(
            timezone.datetime.combine(self.today, time(10, 11)),
            timezone.get_current_timezone(),
        )
        with patch("apps.clinic.twilio_sms.send_sms", return_value="SM123") as send_mock:
            result = process_late_checkin_sms()
        self.assertEqual(result["sms_sent"], 1)
        send_mock.assert_called_once()
        body = send_mock.call_args.kwargs["body"]
        self.assertIn("on your way", body.lower())
        self.appt.refresh_from_db()
        self.assertIsNotNone(self.appt.late_checkin_sms_at)

    @patch("apps.clinic.late_checkin_sms.clinic_now")
    def test_skips_before_threshold(self, mock_now):
        mock_now.return_value = timezone.make_aware(
            timezone.datetime.combine(self.today, time(10, 5)),
            timezone.get_current_timezone(),
        )
        with patch("apps.clinic.twilio_sms.send_sms") as send_mock:
            result = process_late_checkin_sms()
        self.assertEqual(result["sms_sent"], 0)
        send_mock.assert_not_called()
        self.appt.refresh_from_db()
        self.assertIsNone(self.appt.late_checkin_sms_at)

    @patch("apps.clinic.late_checkin_sms.clinic_now")
    def test_skips_when_already_checked_in(self, mock_now):
        self.appt.status = Appointment.Status.CHECKED_IN
        self.appt.save(update_fields=["status", "updated_at"])
        mock_now.return_value = timezone.make_aware(
            timezone.datetime.combine(self.today, time(10, 15)),
            timezone.get_current_timezone(),
        )
        with patch("apps.clinic.twilio_sms.send_sms") as send_mock:
            result = process_late_checkin_sms()
        self.assertEqual(result["sms_sent"], 0)
        send_mock.assert_not_called()

    @patch("apps.clinic.late_checkin_sms.clinic_now")
    def test_sends_only_once(self, mock_now):
        mock_now.return_value = timezone.make_aware(
            timezone.datetime.combine(self.today, time(10, 15)),
            timezone.get_current_timezone(),
        )
        with patch("apps.clinic.twilio_sms.send_sms", return_value="SM123"):
            process_late_checkin_sms()
        with patch("apps.clinic.twilio_sms.send_sms") as send_mock:
            result = process_late_checkin_sms()
        self.assertEqual(result["sms_sent"], 0)
        send_mock.assert_not_called()

    def test_threshold_helper(self):
        appt_start = timezone.make_aware(
            timezone.datetime.combine(self.today, time(10, 0)),
            timezone.get_current_timezone(),
        )
        before = appt_start + timedelta(minutes=9)
        after = appt_start + timedelta(minutes=10)
        with patch("apps.clinic.late_checkin_sms.clinic_now", return_value=before):
            self.assertFalse(appointment_past_late_checkin_threshold(self.appt))
        with patch("apps.clinic.late_checkin_sms.clinic_now", return_value=after):
            self.assertTrue(appointment_past_late_checkin_threshold(self.appt))
