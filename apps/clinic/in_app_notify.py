"""Create in-app staff notifications (doctor bell) after DB commit."""

from __future__ import annotations

from datetime import date as date_type
from datetime import time as time_type

from .models import Appointment, Provider, StaffNotification
from .utils import format_time_12h


def create_checkin_in_app_notification(appointment_id: int) -> None:
    appt = (
        Appointment.objects.select_related("patient", "provider")
        .filter(pk=appointment_id)
        .first()
    )
    if not appt:
        return
    patient = f"{appt.patient.first_name} {appt.patient.last_name}".strip() or "A patient"
    msg = f"{patient} completed check-in at the kiosk for {format_time_12h(appt.start_time)} today."
    StaffNotification.objects.create(
        recipient_id=appt.provider.user_id,
        kind=StaffNotification.Kind.CHECKIN,
        message=msg,
        appointment=appt,
    )


def create_new_booking_in_app_notification(appointment_id: int) -> None:
    appt = (
        Appointment.objects.select_related("patient", "provider", "booked_service")
        .filter(pk=appointment_id)
        .first()
    )
    if not appt:
        return
    patient = f"{appt.patient.first_name} {appt.patient.last_name}".strip() or "A patient"
    service_name = appt.booked_service.name if appt.booked_service else "appointment"
    date_disp = appt.appointment_date.strftime("%a %b %d, %Y")
    time_disp = format_time_12h(appt.start_time)
    msg = f"New booking: {patient}, {service_name}, {date_disp} at {time_disp}."
    StaffNotification.objects.create(
        recipient_id=appt.provider.user_id,
        kind=StaffNotification.Kind.NEW_BOOKING,
        message=msg,
        appointment=appt,
    )


def create_schedule_change_in_app_notifications(
    appointment_id: int,
    change_lines: list[str],
    old_provider_id: int | None,
    old_date_iso: str | None,
    old_time_iso: str | None,
) -> None:
    appt = (
        Appointment.objects.select_related("patient", "provider", "booked_service")
        .filter(pk=appointment_id)
        .first()
    )
    if not appt or not change_lines:
        return

    patient = f"{appt.patient.first_name} {appt.patient.last_name}".strip() or "A patient"
    date_disp = appt.appointment_date.strftime("%a %b %d, %Y")
    time_disp = format_time_12h(appt.start_time)

    if (
        old_provider_id
        and old_provider_id != appt.provider_id
        and old_date_iso
        and old_time_iso
    ):
        prev = Provider.objects.filter(pk=old_provider_id).select_related("user").first()
        if prev:
            try:
                od = date_type.fromisoformat(old_date_iso)
                ot = time_type.fromisoformat(old_time_iso)
            except ValueError:
                od, ot = appt.appointment_date, appt.start_time
            away = (
                f"{patient} was moved to another provider "
                f"(was {od.strftime('%a %b %d, %Y')} {format_time_12h(ot)})."
            )
            StaffNotification.objects.create(
                recipient_id=prev.user_id,
                kind=StaffNotification.Kind.REASSIGNED_AWAY,
                message=away,
                appointment=appt,
            )

    detail = " ".join(change_lines)
    msg = f"Schedule update — {patient} on {date_disp} at {time_disp}: {detail}"
    StaffNotification.objects.create(
        recipient_id=appt.provider.user_id,
        kind=StaffNotification.Kind.SCHEDULE_CHANGE,
        message=msg,
        appointment=appt,
    )
