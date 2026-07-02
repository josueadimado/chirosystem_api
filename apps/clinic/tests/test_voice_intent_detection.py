"""Voice relay off-script intent detection (cancel/reschedule vs info-only)."""

from django.test import SimpleTestCase

from voice_relay import _detect_appointment_info_intent, _detect_cancel_reschedule_intent


class VoiceIntentDetectionTests(SimpleTestCase):
    def test_info_when_is_my_appointment(self):
        self.assertTrue(_detect_appointment_info_intent("When is my appointment?"))
        self.assertFalse(_detect_cancel_reschedule_intent("When is my appointment?")[0])

    def test_change_the_appointment_is_reschedule_not_info(self):
        self.assertTrue(_detect_cancel_reschedule_intent("I need to change the appointment")[0])
        self.assertFalse(_detect_appointment_info_intent("I need to change the appointment"))

    def test_different_time_with_my_appointment_is_reschedule_not_info(self):
        speech = "My appointment needs a different time"
        self.assertTrue(_detect_cancel_reschedule_intent(speech)[0])
        self.assertFalse(_detect_appointment_info_intent(speech))

    def test_another_time_phrase_not_info_only(self):
        speech = "Can we do my appointment at another time?"
        self.assertTrue(_detect_cancel_reschedule_intent(speech)[0])
        self.assertFalse(_detect_appointment_info_intent(speech))

    def test_different_day_phrase_not_info_only(self):
        speech = "I want a different day for my appointment"
        self.assertTrue(_detect_cancel_reschedule_intent(speech)[0])
        self.assertFalse(_detect_appointment_info_intent(speech))
