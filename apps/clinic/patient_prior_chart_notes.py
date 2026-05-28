"""Prior-visit chart context for doctors (handoff reminders vs consultation SOAP notes)."""

from __future__ import annotations

from django.db.models import Q

from .models import Appointment, Visit


def prior_chart_notes_for_appointment(
    appointment: Appointment,
    *,
    limit: int = 8,
) -> list[dict[str, object]]:
    """
    Earlier appointments for the same patient (before this visit's date/time).

    Returns handoff/reminder text from the appointment row and consultation notes from the linked visit.
    """
    prior_q = Q(appointment_date__lt=appointment.appointment_date) | Q(
        appointment_date=appointment.appointment_date,
        start_time__lt=appointment.start_time,
    )
    qs = (
        Appointment.objects.filter(patient_id=appointment.patient_id)
        .filter(prior_q)
        .exclude(pk=appointment.pk)
        .exclude(
            status__in=[
                Appointment.Status.CANCELLED,
                Appointment.Status.NO_SHOW,
            ],
        )
        .select_related("provider", "booked_service")
        .prefetch_related("visit")
        .order_by("-appointment_date", "-start_time")[:limit]
    )
    out: list[dict[str, object]] = []
    for appt in qs:
        handoff = (appt.clinical_handoff_notes or "").strip()
        clinical = ""
        try:
            visit = appt.visit
            if visit:
                clinical = (visit.doctor_notes or "").strip()
        except Visit.DoesNotExist:
            pass
        if not handoff and not clinical:
            continue
        out.append(
            {
                "appointment_id": appt.id,
                "appointment_date": appt.appointment_date.isoformat(),
                "start_time": appt.start_time.strftime("%I:%M %p").lstrip("0"),
                "provider_name": str(appt.provider),
                "service_name": appt.booked_service.name if appt.booked_service else "",
                "status": appt.status,
                "handoff_notes": handoff,
                "clinical_notes": clinical,
            }
        )
    return out
