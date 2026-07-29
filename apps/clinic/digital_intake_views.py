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


class PublicDigitalIntakeViewSet(viewsets.ViewSet):
    """Unauthenticated patient intake via secret personal link."""

    permission_classes = [AllowAny]
    authentication_classes = []

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
        limit = int(request.query_params.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    rows = search_submissions(q=q, form_type=form_type, status=status_filter, limit=limit)
    return Response(
        {
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
