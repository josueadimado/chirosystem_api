"""Voice AI errors logged to SystemErrorLog for Admin → Errors."""

from django.test import TestCase

from apps.clinic.error_tracking import capture_voice_ai_error
from apps.clinic.models import SystemErrorLog


class VoiceAiErrorTrackingTests(TestCase):
    def test_capture_voice_ai_error_persists_row(self):
        pk = capture_voice_ai_error(
            message="Realtime tool book_appointment: slot taken",
            channel="realtime",
            operation="tool:book_appointment",
            call_sid="CA1234567890",
            from_number="+15555550123",
            level="warning",
            exception_type="VoiceTool:book_appointment",
        )
        self.assertIsNotNone(pk)
        row = SystemErrorLog.objects.get(pk=pk)
        self.assertEqual(row.source, "voice_ai")
        self.assertEqual(row.level, "warning")
        self.assertIn("/voice/realtime/", row.path)
        self.assertEqual(row.http_method, "WS")
        self.assertEqual(row.extra.get("category"), "voice_ai")
        self.assertEqual(row.extra.get("channel"), "realtime")
        self.assertTrue(row.extra.get("from_number", "").startswith("***"))
