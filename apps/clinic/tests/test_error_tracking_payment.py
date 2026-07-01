"""Payment failures logged to SystemErrorLog."""

from django.test import RequestFactory, TestCase

from apps.clinic.error_tracking import capture_payment_failure
from apps.clinic.models import SystemErrorLog


class CapturePaymentFailureTests(TestCase):
    def test_saved_card_failure_creates_warning_log(self):
        factory = RequestFactory()
        request = factory.post(
            "/api/v1/doctor/charge-saved-card/",
            data={"invoice_id": 99},
            content_type="application/json",
        )
        pk = capture_payment_failure(
            request=request,
            operation="charge_saved_card",
            detail="Nothing is owed on this invoice — refresh the page or use Check Square if payment already went through.",
            error_code="nothing_due",
            invoice_id=99,
            patient_id=60,
        )
        self.assertIsNotNone(pk)
        row = SystemErrorLog.objects.get(pk=pk)
        self.assertEqual(row.level, "warning")
        self.assertIn("Nothing is owed", row.message)
        self.assertEqual(row.extra.get("category"), "payment")
        self.assertEqual(row.extra.get("invoice_id"), 99)
        self.assertEqual(row.extra.get("error_code"), "nothing_due")
