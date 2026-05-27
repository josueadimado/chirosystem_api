"""Helpers for patient phone matching (households may share one number)."""

from __future__ import annotations

import logging

from .models import Patient
from .utils import normalize_phone

logger = logging.getLogger(__name__)


def patient_matches_phone_normalized(patient: Patient, phone_normalized: str) -> bool:
    """
    True if this patient's phone matches ``phone_normalized`` using the same rules as
    ``patients_matching_phone`` (exact stored E.164 match, or legacy formatted numbers
    that normalize to the same value).
    """
    if not (phone_normalized or "").strip():
        return False
    if (patient.phone or "").strip() == phone_normalized:
        return True
    try:
        return normalize_phone(patient.phone) == phone_normalized
    except Exception:
        logger.warning(
            "normalize_phone failed for patient_id=%s",
            getattr(patient, "pk", None),
            exc_info=True,
        )
        return False


def patients_matching_phone(phone_normalized: str) -> list[Patient]:
    """
    All patients whose phone matches ``phone_normalized``: exact DB match first,
    then a legacy scan for rows stored in an older format.
    """
    if not (phone_normalized or "").strip():
        return []
    exact = list(Patient.objects.filter(phone=phone_normalized).order_by("pk"))
    if exact:
        return exact
    out: list[Patient] = []
    for p in Patient.objects.only("id", "phone").iterator():
        if normalize_phone(p.phone) == phone_normalized:
            out.append(p)
    out.sort(key=lambda x: x.pk)
    return out


def names_equal_casefold(p: Patient, first_name: str, last_name: str) -> bool:
    fn = (first_name or "").strip().casefold()
    ln = (last_name or "").strip().casefold()
    if not fn or not ln:
        return False
    return p.first_name.strip().casefold() == fn and p.last_name.strip().casefold() == ln


def find_duplicate_patient(
    *,
    first_name: str,
    last_name: str,
    phone: str,
    date_of_birth,
    exclude_pk: int | None = None,
) -> Patient | None:
    """
    Same person when first name, last name, date of birth, and phone all match
    (names case-insensitive; phone compared after normalization).
    """
    if date_of_birth is None:
        return None
    fn = (first_name or "").strip()
    ln = (last_name or "").strip()
    if not fn or not ln:
        return None
    try:
        phone_n = normalize_phone(phone or "")
    except Exception:
        logger.warning("normalize_phone failed during duplicate patient check", exc_info=True)
        return None
    if not phone_n:
        return None

    qs = Patient.objects.filter(date_of_birth=date_of_birth)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)

    for p in qs.iterator(chunk_size=200):
        if names_equal_casefold(p, fn, ln) and patient_matches_phone_normalized(p, phone_n):
            return p
    return None


def duplicate_patient_message(existing: Patient, *, updating: bool = False) -> str:
    base = (
        "A patient with the same first name, last name, date of birth, and phone number "
        f"already exists (profile #{existing.pk}: {existing.first_name} {existing.last_name})."
    )
    if updating:
        return f"{base} Change the phone, name, or date of birth so they don't match that record, or open profile #{existing.pk} instead."
    return f"{base} Open that record instead of creating a duplicate."


def resolve_patient_profile_duplicate(
    *,
    first_name: str,
    last_name: str,
    phone: str,
    date_of_birth,
    exclude_pk: int | None,
) -> Patient | None:
    """Return conflicting patient if this profile would duplicate another."""
    return find_duplicate_patient(
        first_name=first_name,
        last_name=last_name,
        phone=phone,
        date_of_birth=date_of_birth,
        exclude_pk=exclude_pk,
    )


def get_or_create_patient_for_public_booking(
    *,
    phone_normalized: str,
    first_name: str,
    last_name: str,
    email: str = "",
    date_of_birth=None,
) -> Patient:
    """
    Find or create the patient row for this booking. Same number may exist for a parent
    and a child; we key by normalized phone + first + last name (case-insensitive).
    When date_of_birth is provided, also blocks duplicate profiles (name + DOB + phone).
    """
    fn = (first_name or "").strip()
    ln = (last_name or "").strip()
    if not fn or not ln:
        raise ValueError("first and last name are required for booking.")

    if date_of_birth is not None:
        dup = find_duplicate_patient(
            first_name=fn,
            last_name=ln,
            phone=phone_normalized,
            date_of_birth=date_of_birth,
        )
        if dup is not None:
            em = (email or "").strip()
            if em and em != (dup.email or "").strip():
                dup.email = em
                dup.save(update_fields=["email", "updated_at"])
            return dup

    candidates = patients_matching_phone(phone_normalized)
    em = (email or "").strip()
    for p in candidates:
        if names_equal_casefold(p, fn, ln):
            if em and em != (p.email or "").strip():
                p.email = em
                p.save(update_fields=["email", "updated_at"])
            return p

    return Patient.objects.create(
        phone=phone_normalized,
        first_name=fn,
        last_name=ln,
        email=em,
        date_of_birth=date_of_birth,
    )
