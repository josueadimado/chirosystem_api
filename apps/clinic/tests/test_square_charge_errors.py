"""Staff-facing Square charge error messages."""

from django.test import SimpleTestCase

from apps.clinic.square_helpers import (
    format_save_card_exception,
    format_square_exception,
    looks_like_technical_square_error,
    square_environment_mismatch_warning,
    square_error_list_message,
    square_web_sdk_environment,
)
from apps.clinic.square_payment import charge_saved_card_error_message


class _FakeSquareErr:
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail


class _FakeApiError(Exception):
    def __init__(self, errors, status_code=400, body=None):
        self.errors = errors
        self.status_code = status_code
        self.body = body


class SquareChargeErrorMessageTests(SimpleTestCase):
    def test_internal_nothing_due_message(self):
        msg = charge_saved_card_error_message("nothing_due")
        self.assertIn("Nothing is owed", msg)

    def test_internal_amount_below_minimum_message(self):
        msg = charge_saved_card_error_message("amount_below_minimum")
        self.assertIn("$1.00", msg)

    def test_square_card_declined_code(self):
        msg = charge_saved_card_error_message("Authorization error: card_declined (CARD_DECLINED)")
        self.assertIn("declined", msg.lower())

    def test_technical_headers_never_shown(self):
        raw = "headers: {'date': 'Wed', 'square-version': '2026-05-20', 'cf-ray': 'abc'}"
        self.assertTrue(looks_like_technical_square_error(raw))
        msg = charge_saved_card_error_message(raw)
        self.assertNotIn("headers", msg.lower())
        self.assertNotIn("square-version", msg.lower())
        self.assertIn("Square could not", msg)

    def test_format_square_exception_from_api_error(self):
        exc = _FakeApiError([_FakeSquareErr("NOT_FOUND", "Card not found")], status_code=404)
        msg = format_square_exception(exc)
        self.assertIn("Card not found", msg)
        self.assertIn("NOT_FOUND", msg)

    def test_format_square_exception_hides_header_dump(self):
        exc = Exception("headers: {'square-version': '2026-05-20', 'content-type': 'application/json'}")
        msg = format_square_exception(exc)
        self.assertNotIn("headers", msg.lower())

    def test_square_error_list_message_from_dict(self):
        msg = square_error_list_message([{"code": "CARD_DECLINED", "detail": "Card declined."}])
        self.assertIn("Card declined", msg)
        self.assertIn("CARD_DECLINED", msg)

    def test_format_save_card_invalid_card_data_sandbox_hint(self):
        exc = _FakeApiError([_FakeSquareErr("INVALID_CARD_DATA", "Invalid card data.")], status_code=400)
        msg = format_save_card_exception(exc)
        self.assertIn("invalid card data", msg.lower())
        self.assertNotIn("headers", msg.lower())

    def test_format_save_card_hides_header_dump(self):
        exc = Exception("headers: {'square-version': '2026-05-20', 'content-type': 'application/json'}")
        msg = format_save_card_exception(exc)
        self.assertNotIn("headers", msg.lower())
        self.assertIn("could not save", msg.lower())
