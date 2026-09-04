"""Merge duplicate patient charts into one keep chart (admin, staff, or doctor)."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.clinic.models import (
    Appointment,
    AppointmentSeries,
    Invoice,
    Patient,
    PatientCreditTransaction,
    PatientDocument,
    PatientIntakeAccessToken,
    PatientIntakeSubmission,
    PatientProfileUpdateToken,
    PatientSavedCard,
    Payment,
    Visit,
)
from apps.clinic.patient_phone import names_equal_casefold
from apps.clinic.utils import normalize_phone

logger = logging.getLogger(__name__)

_STRING_FILL_FIELDS = (
    "email",
    "address_line1",
    "address_line2",
    "city_state_zip",
    "emergency_contact_name",
    "emergency_contact_phone",
    "marital_status",
    "sex",
    "insurance_payer_name",
    "insurance_member_id",
    "insurance_group_number",
    "insurance_plan_type",
    "insurance_relationship",
    "insured_name",
    "payment_profile",
)


def _patient_summary(p: Patient) -> dict[str, Any]:
    dob = p.date_of_birth.isoformat() if p.date_of_birth else ""
    return {
        "id": p.id,
        "first_name": p.first_name or "",
        "last_name": p.last_name or "",
        "phone": p.phone or "",
        "email": p.email or "",
        "date_of_birth": dob,
        "address_line1": p.address_line1 or "",
        "city_state_zip": p.city_state_zip or "",
        "credit_balance": str(p.credit_balance or Decimal("0")),
        "has_saved_card": bool((p.square_card_id or "").strip() and (p.card_last4 or "").strip()),
        "card_brand": p.card_brand or "",
        "card_last4": p.card_last4 or "",
        "payment_profile": p.payment_profile or "",
    }


def _count_for(model: type, patient: Patient) -> int:
    return model.objects.filter(patient=patient).count()


def _relation_counts(patient: Patient) -> dict[str, int]:
    return {
        "documents": _count_for(PatientDocument, patient),
        "intake_submissions": _count_for(PatientIntakeSubmission, patient),
        "intake_access_tokens": _count_for(PatientIntakeAccessToken, patient),
        "profile_update_tokens": _count_for(PatientProfileUpdateToken, patient),
        "appointment_series": _count_for(AppointmentSeries, patient),
        "appointments": _count_for(Appointment, patient),
        "visits": _count_for(Visit, patient),
        "invoices": _count_for(Invoice, patient),
        "payments": _count_for(Payment, patient),
        "credit_transactions": _count_for(PatientCreditTransaction, patient),
        "saved_cards": PatientSavedCard.objects.filter(patient=patient, enabled=True).count(),
    }


def _is_blank_str(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _phones_match(a: Patient, b: Patient) -> bool:
    try:
        na = normalize_phone(a.phone or "")
        nb = normalize_phone(b.phone or "")
    except Exception:
        return (a.phone or "").strip() == (b.phone or "").strip()
    return bool(na and nb and na == nb)


def _build_warnings(keep: Patient, discard: Patient) -> list[str]:
    warnings: list[str] = []
    same_name = names_equal_casefold(keep, discard.first_name, discard.last_name)
    if not same_name:
        warnings.append(
            "Names are different. Only merge if these are the same person "
            "(for example a spelling fix), not two family members."
        )
    if _phones_match(keep, discard) and not same_name:
        warnings.append(
            "These charts share a phone number but have different names — "
            "this is often a household, not a duplicate. Double-check before merging."
        )
    if keep.date_of_birth and discard.date_of_birth and keep.date_of_birth != discard.date_of_birth:
        warnings.append(
            "Dates of birth differ. Confirm these are truly the same person before merging."
        )

    kc = (keep.square_customer_id or "").strip()
    dc = (discard.square_customer_id or "").strip()
    kcard = (keep.square_card_id or "").strip()
    dcard = (discard.square_card_id or "").strip()
    if kc and dc and kc != dc:
        warnings.append(
            "Both charts have Square customer IDs. The keep chart’s card will stay; "
            "the other Square customer may become unused in Square."
        )
    if kcard and dcard and kcard != dcard:
        warnings.append(
            "Both charts have a card on file. The keep chart’s card will be used after the merge."
        )

    disc_credit = discard.credit_balance or Decimal("0")
    if disc_credit > 0:
        warnings.append(
            f"Prepaid credit of ${disc_credit} on the duplicate will be added to the keep chart."
        )

    return warnings


def _field_plan(keep: Patient, discard: Patient) -> list[dict[str, str]]:
    """Describe which scalar fields will change on keep."""
    plan: list[dict[str, str]] = []

    def add(field: str, keep_val: str, discard_val: str, action: str) -> None:
        plan.append(
            {
                "field": field,
                "keep_value": keep_val,
                "discard_value": discard_val,
                "action": action,
            }
        )

    # Phone: keep if usable, else take discard
    keep_phone = (keep.phone or "").strip()
    disc_phone = (discard.phone or "").strip()
    if len("".join(c for c in keep_phone if c.isdigit())) >= 10:
        add("phone", keep_phone, disc_phone, "keep")
    elif disc_phone:
        add("phone", keep_phone, disc_phone, "take_from_discard")
    else:
        add("phone", keep_phone, disc_phone, "keep")

    for field in _STRING_FILL_FIELDS:
        kv = getattr(keep, field) or ""
        dv = getattr(discard, field) or ""
        if _is_blank_str(kv) and not _is_blank_str(dv):
            add(field, str(kv), str(dv), "take_from_discard")
        else:
            add(field, str(kv), str(dv), "keep")

    # DOB
    kd = keep.date_of_birth.isoformat() if keep.date_of_birth else ""
    dd = discard.date_of_birth.isoformat() if discard.date_of_birth else ""
    if keep.date_of_birth is None and discard.date_of_birth is not None:
        add("date_of_birth", kd, dd, "take_from_discard")
    else:
        add("date_of_birth", kd, dd, "keep")

    # date_established: earlier of the two if both set; else fill blank
    ke = keep.date_established
    de = discard.date_established
    if ke and de:
        action = "take_from_discard" if de < ke else "keep"
        add(
            "date_established",
            ke.isoformat(),
            de.isoformat(),
            action,
        )
    elif ke is None and de is not None:
        add("date_established", "", de.isoformat(), "take_from_discard")
    else:
        add(
            "date_established",
            ke.isoformat() if ke else "",
            de.isoformat() if de else "",
            "keep",
        )

    # insurance_company
    kid = keep.insurance_company_id
    did = discard.insurance_company_id
    if kid is None and did is not None:
        add("insurance_company_id", "", str(did), "take_from_discard")
    else:
        add("insurance_company_id", str(kid or ""), str(did or ""), "keep")

    # online_chiro_intake_waived OR
    if discard.online_chiro_intake_waived and not keep.online_chiro_intake_waived:
        add("online_chiro_intake_waived", "False", "True", "take_from_discard")
    else:
        add(
            "online_chiro_intake_waived",
            str(bool(keep.online_chiro_intake_waived)),
            str(bool(discard.online_chiro_intake_waived)),
            "keep",
        )

    # Square card bundle
    if (keep.square_card_id or "").strip():
        add(
            "square_card",
            f"{keep.card_brand} •••• {keep.card_last4}".strip(),
            f"{discard.card_brand} •••• {discard.card_last4}".strip(),
            "keep",
        )
    elif (discard.square_card_id or "").strip():
        add(
            "square_card",
            "",
            f"{discard.card_brand} •••• {discard.card_last4}".strip(),
            "take_from_discard",
        )
    else:
        add("square_card", "", "", "keep")

    if (keep.square_customer_id or "").strip():
        add("square_customer_id", keep.square_customer_id, discard.square_customer_id or "", "keep")
    elif (discard.square_customer_id or "").strip():
        add(
            "square_customer_id",
            "",
            discard.square_customer_id,
            "take_from_discard",
        )
    else:
        add("square_customer_id", "", "", "keep")

    # Credits — always sum
    kb = keep.credit_balance or Decimal("0")
    db = discard.credit_balance or Decimal("0")
    add("credit_balance", str(kb), str(db), "sum")

    return plan


def preview_patient_merge(*, keep_patient_id: int, discard_patient_id: int) -> dict[str, Any]:
    if keep_patient_id == discard_patient_id:
        raise ValueError("Choose two different patient charts to merge.")

    keep = Patient.objects.filter(pk=keep_patient_id).first()
    discard = Patient.objects.filter(pk=discard_patient_id).first()
    if not keep:
        raise ValueError("Keep patient was not found.")
    if not discard:
        raise ValueError("Duplicate (discard) patient was not found.")

    keep_counts = _relation_counts(keep)
    discard_counts = _relation_counts(discard)
    move_total = sum(discard_counts.values())

    return {
        "keep": _patient_summary(keep),
        "discard": _patient_summary(discard),
        "counts": {
            "keep": keep_counts,
            "discard": discard_counts,
            "will_move": discard_counts,
            "move_total": move_total,
        },
        "field_plan": _field_plan(keep, discard),
        "warnings": _build_warnings(keep, discard),
        "detail": (
            f"After merge, chart #{discard.id} will be removed. "
            f"All visits, notes, invoices, and documents from that chart move to #{keep.id}."
        ),
    }


def _apply_scalar_merge(keep: Patient, discard: Patient) -> None:
    """Mutate keep in memory; caller saves."""
    keep_phone_digits = "".join(c for c in (keep.phone or "") if c.isdigit())
    if len(keep_phone_digits) < 10 and (discard.phone or "").strip():
        keep.phone = discard.phone

    for field in _STRING_FILL_FIELDS:
        if _is_blank_str(getattr(keep, field)) and not _is_blank_str(getattr(discard, field)):
            setattr(keep, field, getattr(discard, field))

    if keep.date_of_birth is None and discard.date_of_birth is not None:
        keep.date_of_birth = discard.date_of_birth

    ke = keep.date_established
    de = discard.date_established
    if ke and de:
        keep.date_established = min(ke, de)
    elif ke is None and de is not None:
        keep.date_established = de

    if keep.insurance_company_id is None and discard.insurance_company_id is not None:
        keep.insurance_company_id = discard.insurance_company_id

    if discard.online_chiro_intake_waived:
        keep.online_chiro_intake_waived = True

    # Square: keep unless empty
    if not (keep.square_customer_id or "").strip() and (discard.square_customer_id or "").strip():
        keep.square_customer_id = discard.square_customer_id
    if not (keep.square_card_id or "").strip() and (discard.square_card_id or "").strip():
        keep.square_card_id = discard.square_card_id
        keep.card_brand = discard.card_brand
        keep.card_last4 = discard.card_last4

    # SMS consent: if keep has False and discard True, prefer True (patient opted in somewhere)
    if discard.sms_consent and not keep.sms_consent:
        keep.sms_consent = True
        keep.sms_consent_at = discard.sms_consent_at or timezone.now()

    kb = keep.credit_balance or Decimal("0")
    db = discard.credit_balance or Decimal("0")
    keep.credit_balance = (kb + db).quantize(Decimal("0.01"))
    discard.credit_balance = Decimal("0")


def execute_patient_merge(
    *,
    keep_patient_id: int,
    discard_patient_id: int,
    merged_by=None,
) -> dict[str, Any]:
    preview = preview_patient_merge(
        keep_patient_id=keep_patient_id,
        discard_patient_id=discard_patient_id,
    )

    with transaction.atomic():
        keep = Patient.objects.select_for_update().get(pk=keep_patient_id)
        discard = Patient.objects.select_for_update().get(pk=discard_patient_id)

        moved: dict[str, int] = {}
        moved["documents"] = PatientDocument.objects.filter(patient=discard).update(patient=keep)
        moved["intake_submissions"] = PatientIntakeSubmission.objects.filter(patient=discard).update(
            patient=keep
        )
        moved["intake_access_tokens"] = PatientIntakeAccessToken.objects.filter(patient=discard).update(
            patient=keep
        )
        moved["profile_update_tokens"] = PatientProfileUpdateToken.objects.filter(
            patient=discard
        ).update(patient=keep)
        moved["appointment_series"] = AppointmentSeries.objects.filter(patient=discard).update(
            patient=keep
        )
        moved["appointments"] = Appointment.objects.filter(patient=discard).update(patient=keep)
        moved["visits"] = Visit.objects.filter(patient=discard).update(patient=keep)
        moved["invoices"] = Invoice.objects.filter(patient=discard).update(patient=keep)
        moved["payments"] = Payment.objects.filter(patient=discard).update(patient=keep)
        moved["credit_transactions"] = PatientCreditTransaction.objects.filter(patient=discard).update(
            patient=keep
        )
        # Move cards carefully (unique on patient + square_card_id).
        moved_cards = 0
        for card in list(PatientSavedCard.objects.filter(patient=discard)):
            if PatientSavedCard.objects.filter(patient=keep, square_card_id=card.square_card_id).exists():
                card.enabled = False
                card.is_default = False
                card.patient = keep
                card.save(update_fields=["enabled", "is_default", "patient", "updated_at"])
            else:
                card.patient = keep
                card.save(update_fields=["patient", "updated_at"])
                moved_cards += 1
        moved["saved_cards"] = moved_cards
        # Exactly one default on keep
        defaults = list(
            PatientSavedCard.objects.filter(patient=keep, enabled=True, is_default=True).order_by("-updated_at")
        )
        if len(defaults) > 1:
            for extra in defaults[1:]:
                extra.is_default = False
                extra.save(update_fields=["is_default", "updated_at"])
        from apps.clinic.square_helpers import sync_patient_default_card_cache

        sync_patient_default_card_cache(keep)

        _apply_scalar_merge(keep, discard)
        discard.save(update_fields=["credit_balance", "updated_at"])
        keep.save()

        discard_id = discard.id
        discard_name = f"{discard.first_name} {discard.last_name}".strip()
        discard.delete()

        logger.info(
            "patient_merge keep=%s discard=%s moved=%s by=%s",
            keep.id,
            discard_id,
            moved,
            getattr(merged_by, "id", None),
        )

    return {
        "detail": (
            f"Merged “{discard_name}” (#{discard_id}) into "
            f"“{keep.first_name} {keep.last_name}” (#{keep.id})."
        ),
        "keep_patient_id": keep.id,
        "discard_patient_id": discard_id,
        "moved": moved,
        "warnings": preview.get("warnings") or [],
        "keep": _patient_summary(Patient.objects.get(pk=keep.id)),
    }
