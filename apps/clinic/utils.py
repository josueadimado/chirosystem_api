"""Clinic utilities."""

import re
from decimal import Decimal, InvalidOperation
from typing import Tuple

import phonenumbers


def format_time_12h(t) -> str:
    """Display a datetime.time as e.g. 9:05 AM."""
    h12 = t.hour % 12 or 12
    return f"{h12}:{t.minute:02d} {'PM' if t.hour >= 12 else 'AM'}"


def validate_phone(value: str) -> Tuple[bool, str]:
    """
    Validate phone number using libphonenumber. Returns (valid, e164_or_error).
    Accepts US, Canadian, and international numbers in any format.
    """
    if not value or not str(value).strip():
        return False, "Phone number is required."
    cleaned = re.sub(r"\D", "", str(value))
    if len(cleaned) < 10:
        return False, "Phone number must have at least 10 digits."

    # 10 digits or 11 digits starting with 1: assume US/Canada (NANP)
    if len(cleaned) == 10 or (len(cleaned) == 11 and cleaned.startswith("1")):
        digits = cleaned[-10:]
        try:
            parsed = phonenumbers.parse("+1" + digits, "US")
        except phonenumbers.NumberParseException:
            return False, "Please enter a valid phone number."
    else:
        # International: digits include country code (e.g. 44 for UK, 52 for Mexico)
        try:
            parsed = phonenumbers.parse("+" + cleaned, None)
        except phonenumbers.NumberParseException:
            return False, "Please enter a valid phone number."

    if not phonenumbers.is_valid_number(parsed):
        return False, "Please enter a valid phone number."
    return True, phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def normalize_phone(value: str) -> str:
    """
    Normalize phone to E.164 for storage and matching.
    Handles US (10-digit), NANP (11-digit), and international numbers.
    Legacy 10-digit stored values are treated as US (+1).
    """
    if not value:
        return ""
    digits = re.sub(r"\D", "", str(value))
    if not digits:
        return ""

    # 10 digits: assume US
    if len(digits) == 10:
        try:
            parsed = phonenumbers.parse("+1" + digits, "US")
            if phonenumbers.is_valid_number(parsed):
                return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
        except phonenumbers.NumberParseException:
            pass
        return "+1" + digits  # fallback for legacy
    # 11 digits starting with 1: NANP
    if len(digits) == 11 and digits.startswith("1"):
        try:
            parsed = phonenumbers.parse("+" + digits, None)
            if phonenumbers.is_valid_number(parsed):
                return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
        except phonenumbers.NumberParseException:
            pass
        return "+" + digits
    # International (12+ digits or explicit country code)
    try:
        parsed = phonenumbers.parse("+" + digits, None)
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException:
        pass
    return "+" + digits


def format_usd_plain(amount) -> str:
    """Format a price for SMS/email (e.g. ``$85.00``). Returns empty string if value is missing or invalid."""
    if amount is None:
        return ""
    try:
        d = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError):
        return ""
    return f"${d:,.2f}"


def format_usd_plain(amount) -> str:
    """Format a price for SMS/email (e.g. ``$85.00``). Returns empty string if value is missing or invalid."""
    if amount is None:
        return ""
    try:
        d = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError):
        return ""
    return f"${d:,.2f}"
