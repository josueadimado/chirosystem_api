"""Voice AI transfer number helpers."""

from django.test import SimpleTestCase, override_settings

from apps.clinic.voice_office import (
    DEFAULT_VOICE_TRANSFER_E164,
    voice_transfer_phone_display,
    voice_transfer_phone_e164,
)


class VoiceTransferPhoneTests(SimpleTestCase):
    @override_settings(VOICE_TRANSFER_PHONE_NUMBER="")
    def test_default_transfer_number(self):
        self.assertEqual(voice_transfer_phone_e164(), DEFAULT_VOICE_TRANSFER_E164)
        self.assertIn("921", voice_transfer_phone_display())

    @override_settings(VOICE_TRANSFER_PHONE_NUMBER="+12699216773")
    def test_env_transfer_number(self):
        self.assertEqual(voice_transfer_phone_e164(), "+12699216773")

    @override_settings(VOICE_TRANSFER_PHONE_NUMBER="(269) 921-6773")
    def test_env_transfer_number_formatted(self):
        self.assertEqual(voice_transfer_phone_e164(), "+12699216773")
