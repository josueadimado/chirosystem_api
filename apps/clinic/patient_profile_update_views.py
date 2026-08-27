"""Public + staff API for patient profile / card update magic links."""

from __future__ import annotations

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.clinic.models import Patient
from apps.clinic.patient_profile_update import (
    create_profile_update_token,
    get_active_profile_update_token,
    mark_profile_update_token_accessed,
    patient_profile_update_public_url,
    profile_update_payload,
    send_profile_update_link_email,
    send_profile_update_link_sms,
    update_patient_profile_from_payload,
)
from apps.clinic.square_helpers import (
    format_save_card_exception,
    patient_saved_card_display,
    save_card_from_source,
    square_configured,
)


class PublicPatientProfileUpdateViewSet(viewsets.ViewSet):
    """Unauthenticated patient self-update via secret personal link."""

    permission_classes = [AllowAny]
    authentication_classes = []

    @action(detail=False, methods=["get"], url_path=r"session/(?P<token>[^/.]+)")
    def session(self, request, token=None):
        access = get_active_profile_update_token(token or "")
        if not access:
            return Response(
                {"detail": "This update link is invalid or has expired. Ask the clinic for a new link."},
                status=status.HTTP_404_NOT_FOUND,
            )
        mark_profile_update_token_accessed(access)
        return Response(profile_update_payload(access))

    @action(detail=False, methods=["post"], url_path=r"update-profile/(?P<token>[^/.]+)")
    def update_profile(self, request, token=None):
        access = get_active_profile_update_token(token or "")
        if not access:
            return Response(
                {"detail": "This update link is invalid or has expired. Ask the clinic for a new link."},
                status=status.HTTP_404_NOT_FOUND,
            )
        mark_profile_update_token_accessed(access)
        data = request.data if isinstance(request.data, dict) else {}
        err = update_patient_profile_from_payload(access.patient, data)
        if err:
            return Response({"detail": err}, status=status.HTTP_400_BAD_REQUEST)
        access.patient.refresh_from_db()
        return Response(
            {
                "detail": "Your information was saved.",
                **profile_update_payload(access),
            }
        )

    @action(detail=False, methods=["post"], url_path=r"save-card/(?P<token>[^/.]+)")
    def save_card(self, request, token=None):
        access = get_active_profile_update_token(token or "")
        if not access:
            return Response(
                {"detail": "This update link is invalid or has expired. Ask the clinic for a new link."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if not square_configured():
            return Response(
                {"detail": "Card registration is not enabled yet. Please call the clinic."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        mark_profile_update_token_accessed(access)
        source_id = (request.data.get("source_id") or "").strip()
        if not source_id:
            return Response({"detail": "Card token is missing. Please try again."}, status=status.HTTP_400_BAD_REQUEST)
        vtok = (request.data.get("verification_token") or "").strip() or None
        try:
            save_card_from_source(access.patient, source_id, verification_token=vtok)
        except Exception as exc:
            return Response({"detail": format_save_card_exception(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        access.patient.refresh_from_db()
        return Response(
            {
                "detail": "Card saved securely.",
                **patient_saved_card_display(access.patient),
                **profile_update_payload(access),
            }
        )


def staff_profile_update_send_link(request):
    """Create a public update-info link and optionally text/email it."""
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

    try:
        days_valid = int(request.data.get("days_valid") or 14)
    except (TypeError, ValueError):
        days_valid = 14

    access = create_profile_update_token(patient, created_by=request.user, days_valid=days_valid)
    url = patient_profile_update_public_url(access.token)

    send_sms = bool(request.data.get("send_sms", True))
    send_email = bool(request.data.get("send_email", False))

    sms_ok = False
    sms_detail = ""
    email_ok = False
    email_detail = ""

    if send_sms:
        sms_ok, sms_detail = send_profile_update_link_sms(patient, url)
    if send_email:
        email_ok, email_detail = send_profile_update_link_email(patient, url)

    notes = []
    if send_sms:
        notes.append(sms_detail if sms_ok else f"SMS not sent: {sms_detail}")
    if send_email:
        notes.append(email_detail if email_ok else f"Email not sent: {email_detail}")

    return Response(
        {
            "detail": "Update link created." + ((" " + " ".join(notes)) if notes else ""),
            "url": url,
            "token": access.token,
            "expires_at": access.expires_at.isoformat(),
            "sms_sent": sms_ok,
            "sms_detail": sms_detail,
            "email_sent": email_ok,
            "email_detail": email_detail,
        },
        status=status.HTTP_201_CREATED,
    )
