"""SMS one-time code so a patient can open the profile/card update page from booking."""

from __future__ import annotations

import logging
import secrets
from typing import Any

from django.core import signing
from django.core.cache import cache

from apps.clinic.models import ClinicSettings, Patient
from apps.clinic.patient_phone import names_equal_casefold, patients_matching_phone
from apps.clinic.utils import normalize_phone, validate_phone

logger = logging.getLogger(__name__)

_CHALLENGE_SALT = "chiroflow.patient.profile.sms.otp"
_CACHE_PREFIX = "patient_profile_sms_otp:"
_RATE_PREFIX = "patient_profile_sms_otp_rate:"
_CODE_TTL = 600  # 10 minutes
_RATE_TTL = 60  # one send per phone per minute
_MAX_ATTEMPTS = 5


def _clinic_name() -> str:
    try:
        return (ClinicSettings.get_solo().clinic_name or "").strip() or "Relief Chiropractic"
    except Exception:
        return "Relief Chiropractic"


def mask_phone(phone_e164: str) -> str:
    digits = "".join(c for c in (phone_e164 or "") if c.isdigit())
    if len(digits) < 4:
        return "***"
    return f"•••-•••-{digits[-4:]}"


def resolve_patients_for_phone(phone_raw: str) -> tuple[list[Patient], str | None]:
    """
    Returns (patients, error_detail).
    error_detail is set when phone is invalid.
    """
    valid, phone_or_err = validate_phone(phone_raw)
    if not valid:
        return [], phone_or_err or "Enter a valid phone number."
    try:
        norm = normalize_phone(phone_or_err)
    except Exception:
        return [], "Enter a valid phone number."
    return patients_matching_phone(norm), None


def public_patient_match(p: Patient) -> dict[str, Any]:
    return {
        "first_name": p.first_name or "",
        "last_name": p.last_name or "",
    }


def resolve_single_patient(
    phone_raw: str,
    *,
    first_name: str = "",
    last_name: str = "",
) -> tuple[Patient | None, list[Patient], str | None]:
    """
    Returns (patient, household_list, error).
    If multiple share the phone and name is missing/ambiguous, patient is None and household_list is set.
    """
    patients, err = resolve_patients_for_phone(phone_raw)
    if err:
        return None, [], err
    if not patients:
        return None, [], "We could not find a patient with that phone number. Book a visit first, or call the clinic."

    fn = (first_name or "").strip()
    ln = (last_name or "").strip()
    if fn and ln:
        narrowed = [p for p in patients if names_equal_casefold(p, fn, ln)]
        if not narrowed:
            return None, patients, "That name does not match anyone on this phone number. Pick your name from the list."
        return narrowed[0], patients, None

    if len(patients) == 1:
        return patients[0], patients, None

    return None, patients, None


def _rate_key(phone_e164: str) -> str:
    return f"{_RATE_PREFIX}{phone_e164}"


def create_sms_challenge(patient: Patient) -> tuple[str, str]:
    """Store a 6-digit code; return (challenge_token, plaintext_code)."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    cache.set(f"{_CACHE_PREFIX}{patient.pk}", {"code": code, "attempts": 0}, timeout=_CODE_TTL)
    token = signing.dumps({"pid": patient.pk}, salt=_CHALLENGE_SALT)
    return token, code


def send_profile_otp_sms(patient: Patient, code: str) -> tuple[bool, str]:
    from apps.clinic.twilio_sms import send_sms_detailed, sms_footer, twilio_configured

    if not twilio_configured():
        return False, "Text messaging is not configured. Please call the clinic for an update link."

    phone = (patient.phone or "").strip()
    ok, normalized = validate_phone(phone)
    if not ok:
        return False, normalized or "Patient phone is invalid."

    body = (
        f"{_clinic_name()}: Your verification code is {code}. "
        f"It expires in 10 minutes. Use it to update your info & card."
        f"{sms_footer()}"
    )
    _sid, err = send_sms_detailed(to_e164=normalized, body=body)
    if err:
        return False, err
    return True, "Code sent."


def request_sms_code(
    phone_raw: str,
    *,
    first_name: str = "",
    last_name: str = "",
) -> tuple[dict[str, Any] | None, str | None, int]:
    """
    Start OTP. Returns (payload, error, http_status_hint).
    payload may include household_members when name pick is required.
    """
    patient, household, err = resolve_single_patient(
        phone_raw, first_name=first_name, last_name=last_name
    )
    if err and patient is None and not household:
        return None, err, 400

    if patient is None:
        # Ambiguous household — client must pick a name
        return (
            {
                "need_patient_pick": True,
                "household_members": [public_patient_match(p) for p in household],
                "detail": "More than one person uses this phone. Select your name, then request a code.",
            },
            None,
            200,
        )

    ok_phone, phone_e164 = validate_phone(phone_raw)
    if not ok_phone:
        return None, phone_e164 or "Enter a valid phone number.", 400

    if cache.get(_rate_key(phone_e164)):
        return None, "Please wait about a minute before requesting another code.", 429
    cache.set(_rate_key(phone_e164), "1", timeout=_RATE_TTL)

    challenge_token, code = create_sms_challenge(patient)
    sms_ok, sms_detail = send_profile_otp_sms(patient, code)
    if not sms_ok:
        cache.delete(f"{_CACHE_PREFIX}{patient.pk}")
        return None, sms_detail, 503

    return (
        {
            "need_patient_pick": False,
            "challenge_token": challenge_token,
            "masked_phone": mask_phone(phone_e164),
            "patient_first_name": patient.first_name or "",
            "detail": "We texted a verification code to your phone.",
            "expires_in_seconds": _CODE_TTL,
        },
        None,
        200,
    )


def verify_sms_code(challenge_token: str, code: str) -> Patient | None:
    try:
        data = signing.loads(challenge_token, salt=_CHALLENGE_SALT, max_age=_CODE_TTL + 60)
    except signing.BadSignature:
        return None
    try:
        pid = int(data.get("pid"))
    except (TypeError, ValueError):
        return None

    cached = cache.get(f"{_CACHE_PREFIX}{pid}")
    if not isinstance(cached, dict):
        return None
    attempts = int(cached.get("attempts") or 0)
    if attempts >= _MAX_ATTEMPTS:
        cache.delete(f"{_CACHE_PREFIX}{pid}")
        return None
    expected = str(cached.get("code") or "").strip()
    if (code or "").strip() != expected:
        cached["attempts"] = attempts + 1
        cache.set(f"{_CACHE_PREFIX}{pid}", cached, timeout=_CODE_TTL)
        return None

    cache.delete(f"{_CACHE_PREFIX}{pid}")
    return Patient.objects.filter(pk=pid).first()
