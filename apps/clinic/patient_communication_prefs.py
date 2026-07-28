"""Per-patient channel preferences for booking, reminders, and bills."""

from __future__ import annotations

from apps.clinic.models import Patient

# Stored on Patient.notify_* fields
NOTIFY_SMS = "sms"
NOTIFY_EMAIL = "email"
NOTIFY_BOTH = "both"
NOTIFY_NONE = "none"

NOTIFY_CHANNEL_CHOICES = [
    (NOTIFY_SMS, "Text (SMS) only"),
    (NOTIFY_EMAIL, "Email only"),
    (NOTIFY_BOTH, "Text and email"),
    (NOTIFY_NONE, "None"),
]

DEFAULT_NOTIFY_BOOKING = NOTIFY_SMS
DEFAULT_NOTIFY_REMINDERS = NOTIFY_SMS
DEFAULT_NOTIFY_BILLS = NOTIFY_EMAIL


def normalize_notify_channel(value: str | None, *, default: str) -> str:
    v = (value or "").strip().lower()
    if v in (NOTIFY_SMS, NOTIFY_EMAIL, NOTIFY_BOTH, NOTIFY_NONE):
        return v
    return default


def normalize_notify_bills_channel(value: str | None) -> str:
    """Paid receipts are email-based; coerce legacy ``sms`` to ``email``."""
    v = normalize_notify_channel(value, default=DEFAULT_NOTIFY_BILLS)
    if v == NOTIFY_SMS:
        return NOTIFY_EMAIL
    return v


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
    """
    Paid receipts are email-only in the product today (SMS receipts not built yet).

    ``notify_bills="sms"`` is a legacy / dead preference: the chart UI shows "Email"
    for it but used to leave the DB value as ``sms``, which blocked sending. Treat
    ``sms`` like ``email`` when an address is on file so staff can email bills.
    """
    pref = (patient.notify_bills or "").strip().lower()
    if pref == NOTIFY_NONE:
        return False
    if pref == NOTIFY_SMS:
        pref = NOTIFY_EMAIL
    return _pref_includes_email(pref) and bool((patient.email or "").strip())


def patient_wants_bill_sms(patient: Patient) -> bool:
    """No-show fee SMS notices (notify_bills). Visit receipt SMS is not implemented yet."""
    return _pref_includes_sms(patient.notify_bills) and bool((patient.phone or "").strip())


def patient_communication_prefs_payload(patient: Patient) -> dict:
    return {
        "notify_booking": patient.notify_booking,
        "notify_reminders": patient.notify_reminders,
        "notify_bills": patient.notify_bills,
    }
