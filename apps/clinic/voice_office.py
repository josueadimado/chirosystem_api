"""Clinic office phone + Twilio call transfer helpers for voice AI."""

from __future__ import annotations

import logging
from xml.sax.saxutils import escape

import phonenumbers
from django.conf import settings

from apps.clinic.models import ClinicSettings
from apps.clinic.utils import validate_phone

logger = logging.getLogger(__name__)

DEFAULT_OFFICE_E164 = "+12694080303"
DEFAULT_OFFICE_DISPLAY = "+1 (269) 408-0303"
# Live person transfer target for voice AI (never spoken aloud — dial only).
DEFAULT_VOICE_TRANSFER_E164 = "+12699216773"
DEFAULT_VOICE_TRANSFER_DISPLAY = "(269) 921-6773"


def voice_answer_delay_seconds() -> int:
    """Seconds to wait after answer before AI connects (simulates 2–3 rings)."""
    try:
        n = int(getattr(settings, "VOICE_ANSWER_DELAY_SECONDS", 6))
    except (TypeError, ValueError):
        n = 6
    return max(0, min(20, n))


def clinic_office_phone_e164() -> str:
    """Public clinic phone from Admin Settings (SMS / printed notices — not voice transfer)."""
    clinic = ClinicSettings.get_solo()
    raw = (clinic.phone or "").strip() or "2694080303"
    ok, e164 = validate_phone(raw)
    return e164 if ok else DEFAULT_OFFICE_E164


def clinic_office_phone_display() -> str:
    return _format_e164_display(clinic_office_phone_e164(), DEFAULT_OFFICE_DISPLAY)


def _format_e164_display(e164: str, fallback: str) -> str:
    try:
        parsed = phonenumbers.parse(e164, None)
        if parsed.country_code == 1:
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.NATIONAL)
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    except Exception:
        return fallback


def voice_transfer_phone_e164() -> str:
    """
    Number Twilio dials when Sarah transfers to a live person.
    Prefer VOICE_TRANSFER_PHONE_NUMBER env; otherwise default transfer line.
    Never expose this to the AI prompt or spoken dialogue.
    """
    raw = (getattr(settings, "VOICE_TRANSFER_PHONE_NUMBER", None) or "").strip()
    if raw:
        ok, e164 = validate_phone(raw)
        if ok:
            return e164
    return DEFAULT_VOICE_TRANSFER_E164


def voice_transfer_phone_display() -> str:
    """For server logs / admin tooling only — do not speak to callers."""
    return _format_e164_display(voice_transfer_phone_e164(), DEFAULT_VOICE_TRANSFER_DISPLAY)


def clinic_public_address_display(clinic: ClinicSettings | None = None) -> str:
    """Single-line address for voice / public replies (from Admin Settings)."""
    c = clinic or ClinicSettings.get_solo()
    parts = [(c.address_line1 or "").strip(), (c.city_state_zip or "").strip()]
    return ", ".join(p for p in parts if p)


def voice_clinic_display_name(clinic: ClinicSettings | None = None) -> str:
    """Clinic name for spoken greetings (Admin Settings → clinic_name)."""
    c = clinic or ClinicSettings.get_solo()
    return (c.clinic_name or "").strip() or "our office"


def voice_greeting_for_caller(from_number: str, clinic: ClinicSettings | None = None) -> str:
    """
    Build the opening greeting for this caller.
    Returning patient (one phone match): short hello + first name + clinic name.
    New or unknown caller: full Sarah intro with clinic name from settings.
    """
    import random

    from apps.clinic.patient_phone import patients_matching_phone
    from apps.clinic.utils import normalize_phone

    clinic_name = voice_clinic_display_name(clinic)
    norm = normalize_phone(from_number)
    if norm:
        matches = patients_matching_phone(norm)
        if len(matches) == 1:
            return voice_greeting_for_returning_patient(matches[0].first_name, clinic_name)

    intro = voice_greeting_opening(clinic_name)
    closings = [
        "How can I help you today?",
        "What can I help you with today?",
    ]
    return intro + random.choice(closings)


def voice_greeting_for_returning_patient(first_name: str, clinic_name: str) -> str:
    """Short greeting when caller's phone matches one patient on file."""
    import random

    fname = (first_name or "").strip()
    display = fname if fname else "there"
    clinic = (clinic_name or "").strip() or "our office"
    closings = [
        "What can I help you with today?",
        "How can I help you today?",
    ]
    return f"Hello {display}, thank you for calling {clinic}. " + random.choice(closings)


def voice_greeting_opening(clinic_name: str) -> str:
    """Short phone greeting — thank-you, clinic name, Sarah's name."""
    name = (clinic_name or "").strip() or "our office"
    return f"Thank you for calling {name}. This is Sarah. "


def clinic_public_info_prompt_block(
    clinic: ClinicSettings | None = None,
    *,
    include_phone: bool = True,
) -> str:
    """Public clinic facts the voice AI may share (from Admin Settings)."""
    c = clinic or ClinicSettings.get_solo()
    address = clinic_public_address_display(c)
    email = (c.email or "").strip()
    lines = [
        f"Clinic name: {c.clinic_name}",
        f"Address: {address or '(not set in settings)'}",
    ]
    if include_phone:
        lines.append(f"Phone: {clinic_office_phone_display()}")
    if email:
        lines.append(f"Email: {email}")
    return "\n".join(lines)


def transfer_active_call_to_office(call_sid: str) -> dict:
    """
    Redirect an active Twilio call to the live transfer line (VOICE_TRANSFER_PHONE_NUMBER).
    The number is dialed silently — never spoken to the caller.
    """
    sid = (getattr(settings, "TWILIO_ACCOUNT_SID", None) or "").strip()
    token = (getattr(settings, "TWILIO_AUTH_TOKEN", None) or "").strip()
    if not sid or not token:
        return {"ok": False, "error": "Twilio is not configured for call transfer."}

    to_e164 = voice_transfer_phone_e164()
    display = voice_transfer_phone_display()
    from_num = (getattr(settings, "TWILIO_PHONE_NUMBER", None) or "").strip()

    dial_attrs = f'timeout="45" callerId="{escape(from_num)}"' if from_num else 'timeout="45"'
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Say voice="Polly.Joanna">{escape("One moment while I connect you to the front desk.")}</Say>'
        f"<Dial {dial_attrs}>{escape(to_e164)}</Dial>"
        f'<Say voice="Polly.Joanna">{escape("We could not reach the front desk right now. Please try again in a few minutes. Goodbye.")}</Say>'
        "</Response>"
    )

    try:
        from twilio.rest import Client

        Client(sid, token).calls(call_sid).update(twiml=twiml)
        logger.info("Voice transfer [%s] → %s", call_sid[:8], to_e164)
        return {"ok": True, "transferred_to": display, "e164": to_e164}
    except Exception as exc:
        logger.exception("Voice transfer failed call_sid=%s", call_sid[:8])
        return {"ok": False, "error": str(exc)}
