"""Context for booking a follow-up after a completed visit."""

from __future__ import annotations

from .models import Appointment, Visit
from .visit_diagnosis import serialize_visit_diagnoses


def book_next_context_for_appointment(appt: Appointment) -> dict:
    """Summary of the completed visit doctors see when booking the next appointment."""
    visit: Visit | None = None
    try:
        visit = appt.visit
    except Visit.DoesNotExist:
        visit = None

    clinical_notes = ""
    diagnosis_text = ""
    diagnoses: list[dict] = []
    if visit:
        clinical_notes = (visit.doctor_notes or "").strip()
        diagnosis_text = (visit.diagnosis or "").strip()
        diagnoses = serialize_visit_diagnoses(visit)

    return {
        "patient_id": appt.patient_id,
        "patient_name": f"{appt.patient.first_name} {appt.patient.last_name}".strip(),
        "patient_payment_profile": (appt.patient.payment_profile or "").strip(),
        "appointment_id": appt.id,
        "appointment_date": appt.appointment_date.isoformat(),
        "start_time_display": appt.start_time.strftime("%I:%M %p").lstrip("0"),
        "service_name": appt.booked_service.name if appt.booked_service else "",
        "provider_name": str(appt.provider),
        "provider_id": appt.provider_id,
        "handoff_notes": (appt.clinical_handoff_notes or "").strip(),
        "clinical_notes": clinical_notes,
        "diagnosis": diagnosis_text,
        "diagnoses": diagnoses,
    }
