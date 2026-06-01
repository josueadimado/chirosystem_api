"""Per-patient channel preferences for booking, reminders, and bills."""

from __future__ import annotations

from apps.clinic.models import Patient

# Stored on Patient.notify_* fields
NOTIFY_SMS = "sms"
NOTIFY_EMAIL = "email"
NOTIFY_BOTH = "both"

NOTIFY_CHANNEL_CHOICES = [
    (NOTIFY_SMS, "Text (SMS) only"),
    (NOTIFY_EMAIL, "Email only"),
    (NOTIFY_BOTH, "Text and email"),
]

DEFAULT_NOTIFY_BOOKING = NOTIFY_SMS
DEFAULT_NOTIFY_REMINDERS = NOTIFY_SMS
DEFAULT_NOTIFY_BILLS = NOTIFY_EMAIL


def normalize_notify_channel(value: str | None, *, default: str) -> str:
    v = (value or "").strip().lower()
    if v in (NOTIFY_SMS, NOTIFY_EMAIL, NOTIFY_BOTH):
        return v
    return default


def _pref_includes_sms(pref: str) -> bool:
    return pref in (NOTIFY_SMS, NOTIFY_BOTH)


def _pref_includes_email(pref: str) -> bool:
    return pref in (NOTIFY_EMAIL, NOTIFY_BOTH)


def patient_wants_booking_sms(patient: Patient) -> bool:
    """New bookings, reschedules, cancels, and staff book-next confirmations."""
    return _pref_includes_sms(patient.notify_booking) and bool((patient.phone or "").strip())


def patient_wants_booking_email(patient: Patient) -> bool:
    return _pref_includes_email(patient.notify_booking) and bool((patient.email or "").strip())


def patient_wants_reminder_sms(patient: Patient) -> bool:
    """Day-before / same-day reminders (TCPA: still requires sms_consent)."""
    return (
        _pref_includes_sms(patient.notify_reminders)
        and bool((patient.phone or "").strip())
        and patient.sms_consent
    )


def patient_wants_reminder_email(patient: Patient) -> bool:
    return _pref_includes_email(patient.notify_reminders) and bool((patient.email or "").strip())


def patient_wants_bill_email(patient: Patient) -> bool:
    return _pref_includes_email(patient.notify_bills) and bool((patient.email or "").strip())


def patient_wants_bill_sms(patient: Patient) -> bool:
    """No-show fees, visit receipts, and other billing notices (notify_bills)."""
    return _pref_includes_sms(patient.notify_bills) and bool((patient.phone or "").strip())


def patient_communication_prefs_payload(patient: Patient) -> dict:
    return {
        "notify_booking": patient.notify_booking,
        "notify_reminders": patient.notify_reminders,
        "notify_bills": patient.notify_bills,
    }
