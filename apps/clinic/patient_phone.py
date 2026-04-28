"""Helpers for patient phone matching (households may share one number)."""

from __future__ import annotations

from .models import Patient
from .utils import normalize_phone


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


def get_or_create_patient_for_public_booking(
    *,
    phone_normalized: str,
    first_name: str,
    last_name: str,
    email: str = "",
) -> Patient:
    """
    Find or create the patient row for this booking. Same number may exist for a parent
    and a child; we key by normalized phone + first + last name (case-insensitive).
    """
    fn = (first_name or "").strip()
    ln = (last_name or "").strip()
    if not fn or not ln:
        raise ValueError("first and last name are required for booking.")

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
    )
