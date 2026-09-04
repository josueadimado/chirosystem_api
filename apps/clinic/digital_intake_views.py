"""Public + staff API helpers for digital intake forms."""

from __future__ import annotations

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.clinic.digital_intake import (
    FORM_TYPE_LABELS,
    create_intake_access_token,
    form_pack_payload,
    get_active_token,
    mark_token_accessed,
    patient_intake_public_url,
    patient_submissions_summary,
    search_submissions,
    serialize_submission,
    send_intake_link_sms,
    submit_intake_form,
)
from apps.clinic.models import Appointment, Patient, PatientIntakeSubmission
from apps.clinic.patient_phone import names_equal_casefold, patients_matching_phone
from apps.clinic.utils import validate_phone


def _public_match_payload(patient: Patient) -> dict:
    dob = patient.date_of_birth
    return {
        "first_name": patient.first_name,
        "last_name": patient.last_name,
        "date_of_birth": dob.isoformat() if dob else None,
    }


class PublicDigitalIntakeViewSet(viewsets.ViewSet):
    """Unauthenticated patient intake via secret personal link or self-lookup."""

    permission_classes = [AllowAny]
    authentication_classes = []

    @action(detail=False, methods=["get"], url_path="form-types")
    def form_types(self, request):
        return Response(
            {"form_types": [{"value": k, "label": v} for k, v in FORM_TYPE_LABELS.items()]}
        )

    @action(detail=False, methods=["get"], url_path="find-patient")
    def find_patient(self, request):
        """
        Public lookup for intake start: phone required.
        Returns people on that number so the client can pick the right name.
        """
        phone_raw = (request.query_params.get("phone") or "").strip()
        if not phone_raw:
            return Response({"detail": "Phone number is required."}, status=status.HTTP_400_BAD_REQUEST)
        valid, phone_e164 = validate_phone(phone_raw)
        if not valid:
            return Response({"detail": "Enter a valid phone number."}, status=status.HTTP_400_BAD_REQUEST)

        patients = patients_matching_phone(phone_e164)
        if not patients:
            return Response(
                {
                    "found": False,
                    "matches": [],
                    "detail": "We could not find a patient with that phone number. "
                    "Book an appointment first, or call the clinic for help.",
                }
            )

        q = (request.query_params.get("q") or request.query_params.get("name") or "").strip().lower()
        matches = patients
        if q:
            filtered = []
            for p in patients:
                full = f"{p.first_name} {p.last_name}".lower()
                if q in full or q in (p.first_name or "").lower() or q in (p.last_name or "").lower():
                    filtered.append(p)
            matches = filtered

        if not matches:
            return Response(
                {
                    "found": False,
                    "matches": [],
                    "detail": "No one with that name is on this phone number. Check the spelling or pick from the list without a name filter.",
                }
            )

        return Response(
            {
                "found": True,
                "matches": [_public_match_payload(p) for p in matches],
            }
        )

    @action(detail=False, methods=["post"], url_path="public-start")
    def public_start(self, request):
        """
        Start an intake session after the client picks form type + themselves (phone + name).
        Returns a personal /intake/{token} URL.
        """
        form_type = (request.data.get("form_type") or "").strip()
        if form_type not in FORM_TYPE_LABELS:
            return Response({"detail": "Please choose a form type."}, status=status.HTTP_400_BAD_REQUEST)

        phone_raw = (request.data.get("phone") or "").strip()
        valid, phone_e164 = validate_phone(phone_raw)
        if not valid:
            return Response({"detail": "Enter a valid phone number."}, status=status.HTTP_400_BAD_REQUEST)

        first_name = (request.data.get("first_name") or "").strip()
        last_name = (request.data.get("last_name") or "").strip()
        if not first_name or not last_name:
            return Response(
                {"detail": "Select your name so we open the right chart."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        patients = patients_matching_phone(phone_e164)
        matched = [p for p in patients if names_equal_casefold(p, first_name, last_name)]
        if not matched:
            return Response(
                {
                    "detail": "We could not match that name to this phone number. "
                    "Try again or call the clinic."
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        if len(matched) > 1:
            # Same name on one phone is rare; prefer exact DOB if provided.
            dob_raw = (request.data.get("date_of_birth") or "").strip()
            if dob_raw:
                narrowed = [p for p in matched if p.date_of_birth and p.date_of_birth.isoformat() == dob_raw]
                if len(narrowed) == 1:
                    matched = narrowed
            if len(matched) > 1:
                return Response(
                    {
                        "detail": "More than one match — please include your date of birth, or call the clinic.",
                        "need_date_of_birth": True,
                        "matches": [_public_match_payload(p) for p in matched],
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        patient = matched[0]
        access = create_intake_access_token(patient, form_types=[form_type])
        url = patient_intake_public_url(access.token)
        return Response(
            {
                "token": access.token,
                "url": url,
                "form_type": form_type,
                "patient_display_name": f"{patient.first_name} {patient.last_name}".strip(),
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["get"], url_path=r"form/(?P<token>[^/.]+)")
    def form(self, request, token=None):
        access = get_active_token(token or "")
        if access is None:
            return Response(
                {"detail": "This intake link is invalid or has expired. Please ask the clinic for a new link."},
                status=status.HTTP_404_NOT_FOUND,
            )
        mark_token_accessed(access)
        return Response(form_pack_payload(access))

    @action(detail=False, methods=["post"], url_path=r"submit/(?P<token>[^/.]+)")
    def submit(self, request, token=None):
        access = get_active_token(token or "")
        if access is None:
            return Response(
                {"detail": "This intake link is invalid or has expired. Please ask the clinic for a new link."},
                status=status.HTTP_404_NOT_FOUND,
            )
        form_type = (request.data.get("form_type") or "").strip()
        answers = request.data.get("answers") or {}
        if not isinstance(answers, dict):
            return Response({"detail": "answers must be an object."}, status=status.HTTP_400_BAD_REQUEST)
        signature_name = (request.data.get("signature_name") or "").strip()
        save_as_draft = bool(request.data.get("save_as_draft"))
        submission, err = submit_intake_form(
            access,
            form_type=form_type,
            answers=answers,
            signature_name=signature_name,
            save_as_draft=save_as_draft,
        )
        if err:
            return Response({"detail": err}, status=status.HTTP_400_BAD_REQUEST)
        mark_token_accessed(access)
        return Response(
            {
                "detail": "Saved as draft." if save_as_draft else "Thank you — your form was submitted.",
                "submission": serialize_submission(submission) if submission else None,
                "pack": form_pack_payload(access),
            }
        )


def staff_intake_forms_list(request):
    q = (request.query_params.get("q") or "").strip()
    form_type = (request.query_params.get("form_type") or "").strip()
    status_filter = (request.query_params.get("status") or "submitted").strip()
    try:
        page = int(request.query_params.get("page") or 1)
    except (TypeError, ValueError):
        page = 1
    try:
        # Prefer page_size; keep limit as a back-compat alias.
        raw_size = request.query_params.get("page_size") or request.query_params.get("limit") or 30
        page_size = int(raw_size)
    except (TypeError, ValueError):
        page_size = 30
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    offset = (page - 1) * page_size
    rows, total = search_submissions(
        q=q,
        form_type=form_type,
        status=status_filter,
        limit=page_size,
        offset=offset,
    )
    return Response(
        {
            "count": total,
            "page": page,
            "page_size": page_size,
            "results": [serialize_submission(r) for r in rows],
            "form_types": [{"value": k, "label": v} for k, v in FORM_TYPE_LABELS.items()],
        }
    )


def staff_intake_form_detail(request):
    submission_id = request.query_params.get("id") or request.query_params.get("submission_id")
    if not submission_id:
        return Response({"detail": "id is required."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        submission_id = int(submission_id)
    except (TypeError, ValueError):
        return Response({"detail": "Invalid id."}, status=status.HTTP_400_BAD_REQUEST)
    sub = PatientIntakeSubmission.objects.select_related("patient").filter(pk=submission_id).first()
    if not sub:
        return Response({"detail": "Form not found."}, status=status.HTTP_404_NOT_FOUND)
    return Response(serialize_submission(sub))


def staff_patient_intake_forms(request):
    patient_id = request.query_params.get("patient_id")
    if not patient_id:
        return Response({"detail": "patient_id is required."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        patient_id = int(patient_id)
    except (TypeError, ValueError):
        return Response({"detail": "Invalid patient_id."}, status=status.HTTP_400_BAD_REQUEST)
    patient = Patient.objects.filter(pk=patient_id).first()
    if not patient:
        return Response({"detail": "Patient not found."}, status=status.HTTP_404_NOT_FOUND)
    return Response({"patient_id": patient.id, "forms": patient_submissions_summary(patient)})


def staff_intake_send_link(request):
    patient_id = request.data.get("patient_id")
    if not patient_id:
        return Response({"detail": "patient_id is required."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        patient_id = int(patient_id)
    except (TypeError, ValueError):
        return Response({"detail": "Invalid patient_id."}, status=status.HTTP_400_BAD_REQUEST)
    patient = Patient.objects.filter(pk=patient_id).first()
    if not patient:
        return Response({"detail": "Patient not found."}, status=status.HTTP_404_NOT_FOUND)

    form_types = request.data.get("form_types")
    if form_types is not None and not isinstance(form_types, list):
        return Response({"detail": "form_types must be a list."}, status=status.HTTP_400_BAD_REQUEST)

    appointment = None
    appointment_id = request.data.get("appointment_id")
    if appointment_id:
        try:
            appointment_id = int(appointment_id)
        except (TypeError, ValueError):
            return Response({"detail": "Invalid appointment_id."}, status=status.HTTP_400_BAD_REQUEST)
        appointment = Appointment.objects.filter(pk=appointment_id, patient=patient).first()
        if not appointment:
            return Response({"detail": "Appointment not found for this patient."}, status=status.HTTP_404_NOT_FOUND)

    try:
        days_valid = int(request.data.get("days_valid") or 30)
    except (TypeError, ValueError):
        days_valid = 30

    access = create_intake_access_token(
        patient,
        form_types=form_types,
        appointment=appointment,
        created_by=request.user,
        days_valid=days_valid,
    )
    url = patient_intake_public_url(access.token)
    send_sms = bool(request.data.get("send_sms", True))
    sms_ok = False
    sms_detail = ""
    if send_sms:
        sms_ok, sms_detail = send_intake_link_sms(patient, url)

    return Response(
        {
            "detail": "Intake link created." + (f" {sms_detail}" if sms_detail else ""),
            "url": url,
            "token": access.token,
            "expires_at": access.expires_at.isoformat(),
            "form_types": access.form_types,
            "sms_sent": sms_ok,
            "sms_detail": sms_detail,
        },
        status=status.HTTP_201_CREATED,
    )
