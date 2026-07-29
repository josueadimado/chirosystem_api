"""
Digital patient intake forms: routing, prefill, public tokens, and staff payloads.

Form packs:
- massage — Massage Intake Document
- pediatric — Kids Intake (under 18 + chiro)
- adult_chiropractic — New Patient Paperwork 2025
"""

from __future__ import annotations

import secrets
from datetime import date, timedelta
from typing import Any

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from apps.clinic.models import (
    Appointment,
    Patient,
    PatientIntakeAccessToken,
    PatientIntakeSubmission,
    Service,
)
from apps.clinic.patient_demographics import _patient_age_years, apply_patient_intake_validated_data
from apps.clinic.utils import normalize_phone, validate_phone

FORM_TYPE_MASSAGE = PatientIntakeSubmission.FormType.MASSAGE
FORM_TYPE_PEDIATRIC = PatientIntakeSubmission.FormType.PEDIATRIC
FORM_TYPE_ADULT = PatientIntakeSubmission.FormType.ADULT_CHIROPRACTIC

# Keep in sync with PatientIntakeSubmission.FormType labels.
FORM_TYPE_LABELS = dict(PatientIntakeSubmission.FormType.choices)

TOKEN_DEFAULT_DAYS = 30
PEDIATRIC_AGE_CUTOFF = 18


def patient_intake_public_url(token: str) -> str:
    base = (getattr(settings, "FRONTEND_BASE_URL", None) or "").strip().rstrip("/")
    if not base:
        base = "https://book.reliefchiropractic.net"
    return f"{base}/intake/{token}"


def form_types_still_needed(
    patient: Patient,
    *,
    appointment: Appointment | None = None,
    form_types: list[str] | None = None,
) -> list[str]:
    """Recommended forms that are not yet submitted for this patient."""
    recommended = recommend_form_types(patient, explicit=form_types, appointment=appointment)
    needed: list[str] = []
    for ft in recommended:
        prior = latest_submission(patient, ft)
        if prior is None or prior.status != PatientIntakeSubmission.Status.SUBMITTED:
            needed.append(ft)
    return needed


def intake_url_for_appointment_if_needed(appointment: Appointment) -> str:
    """
    Public intake link when this booking still needs paperwork.
    Reuses a recent active token when possible (SMS + email may both call this).
    Empty string when all recommended forms are already submitted.
    """
    patient = appointment.patient
    needed = form_types_still_needed(patient, appointment=appointment)
    if not needed:
        return ""

    now = timezone.now()
    needed_set = set(needed)
    recent = (
        PatientIntakeAccessToken.objects.filter(
            patient=patient,
            revoked_at__isnull=True,
            expires_at__gt=now,
        )
        .order_by("-created_at")[:8]
    )
    for row in recent:
        existing_types = set(row.form_types or [])
        if needed_set <= existing_types or existing_types == needed_set:
            return patient_intake_public_url(row.token)

    access = create_intake_access_token(
        patient,
        form_types=needed,
        appointment=appointment,
    )
    return patient_intake_public_url(access.token)


def recommend_form_types(
    patient: Patient,
    *,
    explicit: list[str] | None = None,
    appointment: Appointment | None = None,
) -> list[str]:
    """Pick which forms this patient should complete."""
    if explicit:
        out = []
        for t in explicit:
            t = (t or "").strip()
            if t in FORM_TYPE_LABELS and t not in out:
                out.append(t)
        if out:
            return out

    types: list[str] = []
    age = _patient_age_years(patient.date_of_birth)

    appointments: list[Appointment] = []
    if appointment is not None:
        appointments = [appointment]
    else:
        today = timezone.localdate()
        appointments = list(
            Appointment.objects.filter(patient=patient)
            .exclude(status=Appointment.Status.CANCELLED)
            .filter(appointment_date__gte=today)
            .select_related("booked_service")
            .order_by("appointment_date", "start_time")[:10]
        )

    for appt in appointments:
        svc = appt.booked_service
        if not svc:
            continue
        if svc.service_type == Service.ServiceType.MASSAGE:
            if FORM_TYPE_MASSAGE not in types:
                types.append(FORM_TYPE_MASSAGE)
        elif svc.service_type == Service.ServiceType.CHIROPRACTIC:
            if age is not None and age < PEDIATRIC_AGE_CUTOFF:
                if FORM_TYPE_PEDIATRIC not in types:
                    types.append(FORM_TYPE_PEDIATRIC)
            else:
                if FORM_TYPE_ADULT not in types:
                    types.append(FORM_TYPE_ADULT)

    # No upcoming visit context: use age (or adult default).
    if not types:
        if age is not None and age < PEDIATRIC_AGE_CUTOFF:
            types.append(FORM_TYPE_PEDIATRIC)
        else:
            types.append(FORM_TYPE_ADULT)

    return types


def _split_city_state_zip(value: str) -> dict[str, str]:
    """Best-effort split of stored city_state_zip into city / state / zip for forms."""
    raw = (value or "").strip()
    if not raw:
        return {"city": "", "state": "", "zip": ""}
    # Common: "St Joseph, MI 49085"
    city, state, zip_code = "", "", ""
    if "," in raw:
        left, right = raw.split(",", 1)
        city = left.strip()
        parts = right.strip().split()
        if parts:
            state = parts[0].strip()
            if len(parts) > 1:
                zip_code = " ".join(parts[1:]).strip()
    else:
        parts = raw.split()
        if len(parts) >= 3 and parts[-1].replace("-", "").isdigit():
            zip_code = parts[-1]
            state = parts[-2]
            city = " ".join(parts[:-2])
        else:
            city = raw
    return {"city": city, "state": state, "zip": zip_code}


def _join_city_state_zip(city: str, state: str, zip_code: str) -> str:
    city = (city or "").strip()
    state = (state or "").strip()
    zip_code = (zip_code or "").strip()
    if city and state and zip_code:
        return f"{city}, {state} {zip_code}"
    if city and state:
        return f"{city}, {state}"
    return " ".join(p for p in (city, state, zip_code) if p).strip()


def patient_prefill(patient: Patient) -> dict[str, Any]:
    """Values already on the patient chart — client may edit any of these."""
    csz = _split_city_state_zip(patient.city_state_zip or "")
    return {
        "first_name": patient.first_name or "",
        "last_name": patient.last_name or "",
        "email": patient.email or "",
        "phone": patient.phone or "",
        "home_phone": "",
        "work_phone": "",
        "date_of_birth": patient.date_of_birth.isoformat() if patient.date_of_birth else "",
        "address_line1": patient.address_line1 or "",
        "address_line2": patient.address_line2 or "",
        "city": csz["city"],
        "state": csz["state"],
        "zip": csz["zip"],
        "city_state_zip": patient.city_state_zip or "",
        "emergency_contact_name": patient.emergency_contact_name or "",
        "emergency_contact_phone": patient.emergency_contact_phone or "",
        "emergency_contact_relationship": "",
        "referred_by": "",
        "physician_name": "",
        "physician_phone": "",
    }


def create_intake_access_token(
    patient: Patient,
    *,
    form_types: list[str] | None = None,
    appointment: Appointment | None = None,
    created_by=None,
    days_valid: int = TOKEN_DEFAULT_DAYS,
) -> PatientIntakeAccessToken:
    recommended = recommend_form_types(patient, explicit=form_types, appointment=appointment)
    token = secrets.token_urlsafe(32)
    return PatientIntakeAccessToken.objects.create(
        patient=patient,
        token=token,
        expires_at=timezone.now() + timedelta(days=max(1, days_valid)),
        form_types=recommended,
        appointment=appointment,
        created_by=created_by if getattr(created_by, "is_authenticated", False) else None,
    )


def get_active_token(raw_token: str) -> PatientIntakeAccessToken | None:
    token = (raw_token or "").strip()
    if not token:
        return None
    row = (
        PatientIntakeAccessToken.objects.select_related("patient", "appointment")
        .filter(token=token, revoked_at__isnull=True)
        .first()
    )
    if row is None or not row.is_active:
        return None
    return row


def mark_token_accessed(access: PatientIntakeAccessToken) -> None:
    PatientIntakeAccessToken.objects.filter(pk=access.pk).update(last_accessed_at=timezone.now())


def latest_submission(patient: Patient, form_type: str) -> PatientIntakeSubmission | None:
    submitted = (
        PatientIntakeSubmission.objects.filter(
            patient=patient,
            form_type=form_type,
            status=PatientIntakeSubmission.Status.SUBMITTED,
        )
        .order_by("-submitted_at", "-updated_at")
        .first()
    )
    if submitted:
        return submitted
    return (
        PatientIntakeSubmission.objects.filter(
            patient=patient,
            form_type=form_type,
            status=PatientIntakeSubmission.Status.DRAFT,
        )
        .order_by("-updated_at")
        .first()
    )


def merge_answers_for_form(patient: Patient, form_type: str) -> dict[str, Any]:
    """Prefill from patient chart, then overlay any prior draft/submitted answers."""
    merged = patient_prefill(patient)
    prior = latest_submission(patient, form_type)
    if prior and isinstance(prior.answers, dict):
        for key, value in prior.answers.items():
            if value is None:
                continue
            if isinstance(value, str) and not value.strip() and merged.get(key):
                continue
            merged[key] = value
    return merged


def form_pack_payload(access: PatientIntakeAccessToken) -> dict[str, Any]:
    patient = access.patient
    form_types = list(access.form_types or []) or recommend_form_types(
        patient, appointment=access.appointment
    )
    forms = []
    for ft in form_types:
        prior = latest_submission(patient, ft)
        forms.append(
            {
                "form_type": ft,
                "label": FORM_TYPE_LABELS.get(ft, ft),
                "status": prior.status if prior else "not_started",
                "submitted_at": prior.submitted_at.isoformat() if prior and prior.submitted_at else None,
                "answers": merge_answers_for_form(patient, ft),
                "signature_name": (prior.signature_name if prior else "") or "",
            }
        )
    return {
        "token": access.token,
        "expires_at": access.expires_at.isoformat(),
        "patient": {
            "id": patient.id,
            "display_name": f"{patient.first_name} {patient.last_name}".strip(),
        },
        "forms": forms,
        "prefill": patient_prefill(patient),
    }


def _normalize_submit_answers(answers: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (answers or {}).items():
        if isinstance(value, str):
            out[key] = value.strip()
        else:
            out[key] = value
    # Rebuild city_state_zip when parts are provided.
    city = str(out.get("city") or "").strip()
    state = str(out.get("state") or "").strip()
    zip_code = str(out.get("zip") or "").strip()
    if city or state or zip_code:
        out["city_state_zip"] = _join_city_state_zip(city, state, zip_code)
    return out


def sync_patient_from_answers(patient: Patient, answers: dict[str, Any]) -> str | None:
    """Update chart demographics from submitted answers. Returns error string or None."""
    data: dict[str, Any] = {}
    if "first_name" in answers and str(answers.get("first_name") or "").strip():
        data["first_name"] = str(answers["first_name"]).strip()
    if "last_name" in answers and str(answers.get("last_name") or "").strip():
        data["last_name"] = str(answers["last_name"]).strip()
    if "email" in answers:
        data["email"] = str(answers.get("email") or "").strip()
    phone_raw = answers.get("phone") or answers.get("mobile_phone") or ""
    if str(phone_raw).strip():
        ok, normalized = validate_phone(str(phone_raw))
        if not ok:
            return normalized
        data["phone"] = normalized
    if "date_of_birth" in answers:
        dob = answers.get("date_of_birth")
        if dob in ("", None):
            data["date_of_birth"] = None
        elif isinstance(dob, date):
            data["date_of_birth"] = dob
        else:
            try:
                data["date_of_birth"] = date.fromisoformat(str(dob)[:10])
            except ValueError:
                return "Date of birth must be a valid date (YYYY-MM-DD)."
    for field in (
        "address_line1",
        "address_line2",
        "city_state_zip",
        "emergency_contact_name",
        "emergency_contact_phone",
    ):
        if field in answers:
            data[field] = str(answers.get(field) or "").strip()

    if not data:
        return None

    err = apply_patient_intake_validated_data(
        patient,
        data,
        allow_identity_fields=True,
    )
    if err:
        return err
    patient.save()
    return None


def submit_intake_form(
    access: PatientIntakeAccessToken,
    *,
    form_type: str,
    answers: dict[str, Any],
    signature_name: str = "",
    save_as_draft: bool = False,
) -> tuple[PatientIntakeSubmission | None, str | None]:
    """Submit or save draft. New submitted rows are appended; drafts are upserted."""
    form_type = (form_type or "").strip()
    if form_type not in FORM_TYPE_LABELS:
        return None, "Unknown form type."

    allowed = list(access.form_types or []) or recommend_form_types(
        access.patient, appointment=access.appointment
    )
    if form_type not in allowed:
        return None, "This form is not part of your intake pack."

    cleaned = _normalize_submit_answers(answers if isinstance(answers, dict) else {})
    sig = (signature_name or "").strip() or str(cleaned.get("signature_name") or "").strip()

    if not save_as_draft:
        if not str(cleaned.get("first_name") or "").strip() or not str(cleaned.get("last_name") or "").strip():
            return None, "First and last name are required."
        if not sig:
            return None, "Please type your full name to sign this form."
        if form_type == FORM_TYPE_ADULT and cleaned.get("policies_acknowledged") is not True:
            return None, "Please check the box that you agree to the clinic policies before signing."
        err = sync_patient_from_answers(access.patient, cleaned)
        if err:
            return None, err

    now = timezone.now()
    if save_as_draft:
        draft = (
            PatientIntakeSubmission.objects.filter(
                patient=access.patient,
                form_type=form_type,
                status=PatientIntakeSubmission.Status.DRAFT,
            )
            .order_by("-updated_at")
            .first()
        )
        if draft:
            draft.answers = cleaned
            draft.signature_name = sig
            draft.appointment = access.appointment
            draft.save(
                update_fields=["answers", "signature_name", "appointment", "updated_at"]
            )
            return draft, None
        submission = PatientIntakeSubmission.objects.create(
            patient=access.patient,
            form_type=form_type,
            status=PatientIntakeSubmission.Status.DRAFT,
            answers=cleaned,
            signature_name=sig,
            appointment=access.appointment,
        )
        return submission, None

    PatientIntakeSubmission.objects.filter(
        patient=access.patient,
        form_type=form_type,
        status=PatientIntakeSubmission.Status.DRAFT,
    ).delete()
    submission = PatientIntakeSubmission.objects.create(
        patient=access.patient,
        form_type=form_type,
        status=PatientIntakeSubmission.Status.SUBMITTED,
        answers=cleaned,
        schema_version=1,
        signature_name=sig,
        signed_at=now,
        submitted_at=now,
        appointment=access.appointment,
    )
    return submission, None


def serialize_submission(sub: PatientIntakeSubmission) -> dict[str, Any]:
    return {
        "id": sub.id,
        "patient_id": sub.patient_id,
        "patient_name": f"{sub.patient.first_name} {sub.patient.last_name}".strip(),
        "patient_phone": sub.patient.phone or "",
        "patient_email": sub.patient.email or "",
        "form_type": sub.form_type,
        "form_label": FORM_TYPE_LABELS.get(sub.form_type, sub.form_type),
        "status": sub.status,
        "answers": sub.answers or {},
        "signature_name": sub.signature_name or "",
        "signed_at": sub.signed_at.isoformat() if sub.signed_at else None,
        "submitted_at": sub.submitted_at.isoformat() if sub.submitted_at else None,
        "updated_at": sub.updated_at.isoformat() if sub.updated_at else None,
        "appointment_id": sub.appointment_id,
    }


def search_submissions(*, q: str = "", form_type: str = "", status: str = "submitted", limit: int = 50):
    qs = PatientIntakeSubmission.objects.select_related("patient").all()
    status = (status or "").strip()
    if status:
        qs = qs.filter(status=status)
    ft = (form_type or "").strip()
    if ft:
        qs = qs.filter(form_type=ft)
    query = (q or "").strip()
    if query:
        phone_digits = normalize_phone(query) if any(c.isdigit() for c in query) else ""
        name_q = Q(patient__first_name__icontains=query) | Q(patient__last_name__icontains=query)
        name_q |= Q(patient__email__icontains=query)
        if " " in query:
            parts = query.split(None, 1)
            if len(parts) == 2:
                name_q |= Q(patient__first_name__icontains=parts[0], patient__last_name__icontains=parts[1])
        if phone_digits:
            name_q |= Q(patient__phone__icontains=phone_digits[-10:])
        qs = qs.filter(name_q)
    return list(qs.order_by("-submitted_at", "-updated_at")[: max(1, min(limit, 100))])


def patient_submissions_summary(patient: Patient) -> list[dict[str, Any]]:
    """Latest row per form type for staff chart."""
    rows = (
        PatientIntakeSubmission.objects.filter(patient=patient)
        .order_by("form_type", "-submitted_at", "-updated_at")
    )
    latest: dict[str, PatientIntakeSubmission] = {}
    for row in rows:
        if row.form_type not in latest:
            latest[row.form_type] = row
    return [serialize_submission(latest[k]) for k in sorted(latest.keys())]


def send_intake_link_sms(patient: Patient, url: str) -> tuple[bool, str]:
    from apps.clinic.twilio_sms import send_sms_detailed, sms_footer

    phone = (patient.phone or "").strip()
    if not phone:
        return False, "Patient has no phone number on file."
    ok, normalized = validate_phone(phone)
    if not ok:
        return False, normalized
    body = (
        f"Relief Chiropractic: Please complete your intake forms before your visit: {url}"
        f"{sms_footer()}"
    )
    _sid, err = send_sms_detailed(to_e164=normalized, body=body)
    if err:
        return False, err
    return True, "Intake link text message sent."
