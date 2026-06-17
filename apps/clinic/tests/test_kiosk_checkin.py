"""Regression tests for kiosk / desk check-in target resolution."""

from __future__ import annotations

from datetime import date, time
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from apps.clinic.models import Appointment
from apps.clinic.views import KioskViewSet


class KioskResolveCheckinTargetsTest(SimpleTestCase):
    """_kiosk_resolve_checkin_targets must only check in explicitly requested visits."""

    def _booked_appt(self, pk: int = 1) -> MagicMock:
        appt = MagicMock(spec=Appointment)
        appt.pk = pk
        appt.id = pk
        appt.status = Appointment.Status.BOOKED
        appt.appointment_date = date.today()
        appt.patient_id = 10
        appt.start_time = time(9, 0)
        return appt

    @patch.object(KioskViewSet, "_can_kiosk_checkin_now", return_value=(True, None, None))
    def test_single_primary_checks_in_only_that_visit(self, _can_now):
        primary = self._booked_appt(1)

        targets, err = KioskViewSet._kiosk_resolve_checkin_targets(
            primary,
            requested_ids=None,
            bypass_early=False,
        )

        self.assertIsNone(err)
        self.assertEqual(targets, [primary])

    @patch.object(KioskViewSet, "_can_kiosk_checkin_now", return_value=(True, None, None))
    @patch("apps.clinic.views.Appointment.objects.filter")
    def test_requested_id_checks_in_only_that_visit(self, mock_filter, _can_now):
        primary = self._booked_appt(1)
        other = self._booked_appt(2)
        mock_filter.return_value.select_related.return_value = [primary]

        targets, err = KioskViewSet._kiosk_resolve_checkin_targets(
            primary,
            requested_ids=[1],
            bypass_early=False,
        )

        self.assertIsNone(err)
        self.assertEqual(targets, [primary])
        self.assertNotIn(other, targets)

    @patch.object(KioskViewSet, "_can_kiosk_checkin_now", return_value=(True, None, None))
    def test_rejects_multiple_requested_ids(self, _can_now):
        primary = self._booked_appt(1)

        targets, err = KioskViewSet._kiosk_resolve_checkin_targets(
            primary,
            requested_ids=[1, 2],
            bypass_early=False,
        )

        self.assertEqual(targets, [])
        self.assertIn("one appointment at a time", err.lower())

    @patch.object(KioskViewSet, "_can_kiosk_checkin_now", return_value=(True, None, None))
    def test_staff_bypass_returns_single_primary(self, _can_now):
        primary = self._booked_appt(1)

        targets, err = KioskViewSet._kiosk_resolve_checkin_targets(
            primary,
            requested_ids=None,
            bypass_early=True,
        )

        self.assertIsNone(err)
        self.assertEqual(targets, [primary])

    @patch.object(KioskViewSet, "_can_kiosk_checkin_now", return_value=(False, None, None))
    def test_too_early_returns_string_error(self, _can_now):
        primary = self._booked_appt(1)

        targets, err = KioskViewSet._kiosk_resolve_checkin_targets(
            primary,
            requested_ids=None,
            bypass_early=False,
        )

        self.assertEqual(targets, [])
        self.assertIsInstance(err, str)
        self.assertIn("too early", err.lower())


class AutoNoShowCountdownTest(SimpleTestCase):
    @patch("apps.clinic.auto_no_show.ClinicSettings.get_cached")
    def test_exempt_visit_not_at_risk(self, mock_settings):
        from apps.clinic.auto_no_show import auto_no_show_countdown_for_appointment
        from apps.clinic.models import Appointment

        mock_settings.return_value = MagicMock(auto_no_show_enabled=True, auto_no_show_grace_minutes=60)

        appt = MagicMock(spec=Appointment)
        appt.status = Appointment.Status.BOOKED
        appt.auto_no_show_exempt = True
        appt.auto_no_show_processed_at = None

        out = auto_no_show_countdown_for_appointment(appt)
        self.assertIsNotNone(out)
        self.assertTrue(out["exempt"])
        self.assertFalse(out["applies"])
