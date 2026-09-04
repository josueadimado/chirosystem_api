"""Magic-link flow: patient updates contact info + Square card on file."""

from __future__ import annotations

import secrets
from datetime import date, timedelta
from typing import Any

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone

from apps.clinic.digital_intake import _join_city_state_zip, _split_city_state_zip, sync_patient_from_answers
from apps.clinic.models import ClinicSettings, Patient, PatientProfileUpdateToken
from apps.clinic.square_helpers import patient_saved_card_display
from apps.clinic.utils import validate_phone

TOKEN_DEFAULT_DAYS = 14


def patient_profile_update_public_url(token: str) -> str:
    base = (getattr(settings, "FRONTEND_BASE_URL", None) or "").strip().rstrip("/")
    if not base:
        base = "http://localhost:3000"
    return f"{base}/update-info/{token}"


def create_profile_update_token(
    patient: Patient,
    *,
    created_by=None,
    days_valid: int = TOKEN_DEFAULT_DAYS,
) -> PatientProfileUpdateToken:
    token = secrets.token_urlsafe(32)
    return PatientProfileUpdateToken.objects.create(
        patient=patient,
        token=token,
        expires_at=timezone.now() + timedelta(days=max(1, min(days_valid, 90))),
        created_by=created_by if getattr(created_by, "is_authenticated", False) else None,
    )


def get_active_profile_update_token(raw_token: str) -> PatientProfileUpdateToken | None:
    token = (raw_token or "").strip()
    if not token:
        return None
    row = (
        PatientProfileUpdateToken.objects.select_related("patient")
        .filter(token=token, revoked_at__isnull=True)
        .first()
    )
    if row is None or not row.is_active:
        return None
    return row


def mark_profile_update_token_accessed(access: PatientProfileUpdateToken) -> None:
    PatientProfileUpdateToken.objects.filter(pk=access.pk).update(last_accessed_at=timezone.now())


def _clinic_display_name() -> str:
    try:
        return (ClinicSettings.get_solo().clinic_name or "").strip() or "Relief Chiropractic"
    except Exception:
        return "Relief Chiropractic"


def profile_update_payload(access: PatientProfileUpdateToken) -> dict[str, Any]:
    patient = access.patient
    csz = _split_city_state_zip(patient.city_state_zip or "")
    card = patient_saved_card_display(patient)
    return {
        "clinic_name": _clinic_display_name(),
        "expires_at": access.expires_at.isoformat(),
        "patient": {
            "first_name": patient.first_name or "",
            "last_name": patient.last_name or "",
            "email": patient.email or "",
            "phone": patient.phone or "",
            "date_of_birth": patient.date_of_birth.isoformat() if patient.date_of_birth else "",
            "address_line1": patient.address_line1 or "",
            "address_line2": patient.address_line2 or "",
            "city": csz.get("city") or "",
            "state": csz.get("state") or "",
            "zip": csz.get("zip") or "",
            "emergency_contact_name": patient.emergency_contact_name or "",
            "emergency_contact_phone": patient.emergency_contact_phone or "",
        },
        "card": {
            "has_saved_card": bool(card.get("has_saved_card") or card.get("card_last4")),
            "card_brand": card.get("card_brand") or "",
            "card_last4": card.get("card_last4") or "",
            "card_display_only": bool(card.get("card_display_only")),
            "saved_cards": card.get("saved_cards") or [],
            "default_saved_card_id": card.get("default_saved_card_id"),
        },
    }


def update_patient_profile_from_payload(patient: Patient, data: dict[str, Any]) -> str | None:
    """Apply contact/demographics from the public form. Returns error string or None."""
    answers: dict[str, Any] = {}
    for key in (
        "first_name",
        "last_name",
        "email",
        "phone",
        "date_of_birth",
        "address_line1",
        "address_line2",
        "emergency_contact_name",
        "emergency_contact_phone",
    ):
        if key in data:
            answers[key] = data.get(key)

    city = str(data.get("city") or "").strip()
    state = str(data.get("state") or "").strip()
    zip_code = str(data.get("zip") or "").strip()
    if "city" in data or "state" in data or "zip" in data:
        answers["city"] = city
        answers["state"] = state
        answers["zip"] = zip_code
        answers["city_state_zip"] = _join_city_state_zip(city, state, zip_code)

    if "date_of_birth" in answers and answers.get("date_of_birth") not in ("", None):
        raw = answers["date_of_birth"]
        if isinstance(raw, date):
            answers["date_of_birth"] = raw.isoformat()
        else:
            answers["date_of_birth"] = str(raw)[:10]

    return sync_patient_from_answers(patient, answers)


def send_profile_update_link_sms(patient: Patient, url: str) -> tuple[bool, str]:
    from apps.clinic.twilio_sms import send_sms_detailed, sms_footer

    phone = (patient.phone or "").strip()
    if not phone:
        return False, "Patient has no phone number on file."
    ok, normalized = validate_phone(phone)
    if not ok:
        return False, normalized or "Invalid phone number."

    clinic = _clinic_display_name()
    body = (
        f"{clinic}: Update your contact info or payment card securely here "
        f"(link expires): {url}"
        f"{sms_footer()}"
    )
    _sid, err = send_sms_detailed(to_e164=normalized, body=body)
    if err:
        return False, err
    return True, "SMS sent."


def send_profile_update_link_email(patient: Patient, url: str) -> tuple[bool, str]:
    to_email = (patient.email or "").strip()
    if not to_email or "@" not in to_email:
        return False, "Patient has no email on file."

    host = (getattr(settings, "EMAIL_HOST", "") or "").strip()
    user = (getattr(settings, "EMAIL_HOST_USER", "") or "").strip()
    if not host or not user:
        return False, "Email is not configured on the server."

    clinic = _clinic_display_name()
    from_email = (getattr(settings, "DEFAULT_FROM_EMAIL", "") or "").strip() or user
    subject = f"{clinic}: Update your info & card"
    text = (
        f"Hello {patient.first_name or 'there'},\n\n"
        f"Use this private link to update your contact information or payment card on file:\n"
        f"{url}\n\n"
        f"This link expires soon. If you did not request this, you can ignore this message.\n\n"
        f"— {clinic}\n"
    )
    html = (
        f"<p>Hello {patient.first_name or 'there'},</p>"
        f"<p>Use this private link to update your contact information or payment card on file:</p>"
        f'<p><a href="{url}">{url}</a></p>'
        f"<p>This link expires soon. If you did not request this, you can ignore this message.</p>"
        f"<p>— {clinic}</p>"
    )
    try:
        msg = EmailMultiAlternatives(subject=subject, body=text, from_email=from_email, to=[to_email])
        msg.attach_alternative(html, "text/html")
        msg.send(fail_silently=False)
    except Exception as exc:
        return False, str(exc)[:200]
    return True, f"Email sent to {to_email}."
