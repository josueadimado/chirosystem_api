"""Regression tests for kiosk / desk check-in target resolution."""

from __future__ import annotations

from datetime import date, time
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from apps.clinic.models import Appointment
from apps.clinic.views import KioskViewSet


class KioskResolveCheckinTargetsTest(SimpleTestCase):
    """_kiosk_resolve_checkin_targets must always return (list, str|None)."""

    def _booked_appt(self, pk: int = 1) -> MagicMock:
        appt = MagicMock(spec=Appointment)
        appt.pk = pk
        appt.id = pk
        appt.status = Appointment.Status.BOOKED
        appt.appointment_date = date.today()
        appt.patient_id = 10
        appt.start_time = time(9, 0)
        return appt

    @patch.object(KioskViewSet, "_kiosk_same_day_booked_siblings")
    @patch.object(KioskViewSet, "_can_kiosk_checkin_now", return_value=(True, None, None))
    def test_in_window_returns_tuple_with_none_error(self, _can_now, mock_siblings):
        primary = self._booked_appt(1)
        sibling = self._booked_appt(2)
        mock_siblings.return_value = [primary, sibling]

        result = KioskViewSet._kiosk_resolve_checkin_targets(
            primary,
            requested_ids=None,
            bypass_early=False,
        )

        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        targets, err = result
        self.assertIsNone(err)
        self.assertEqual(len(targets), 2)

    @patch.object(KioskViewSet, "_kiosk_same_day_booked_siblings")
    @patch.object(KioskViewSet, "_can_kiosk_checkin_now", return_value=(True, None, None))
    def test_staff_bypass_returns_tuple(self, _can_now, mock_siblings):
        primary = self._booked_appt(1)
        mock_siblings.return_value = [primary]

        result = KioskViewSet._kiosk_resolve_checkin_targets(
            primary,
            requested_ids=None,
            bypass_early=True,
        )

        targets, err = result
        self.assertIsNone(err)
        self.assertEqual(targets, [primary])

    @patch.object(KioskViewSet, "_kiosk_same_day_booked_siblings")
    @patch.object(KioskViewSet, "_can_kiosk_checkin_now", return_value=(False, None, None))
    def test_too_early_returns_string_error(self, _can_now, mock_siblings):
        primary = self._booked_appt(1)
        mock_siblings.return_value = [primary]

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
