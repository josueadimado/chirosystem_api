"""
Shared logic for creating an appointment from the public booking payload.

Used by the REST `book` action and by the Twilio voice assistant webhook.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from rest_framework.exceptions import ValidationError as RestValidationError

from .booking_availability import provider_interval_blocked_online
from .chiropractic_booking_policy import chiropractic_booking_must_use_intake
from .models import Appointment, Patient, Provider, Service
from .utils import format_time_12h, normalize_phone


def create_appointment_from_public_booking(validated: dict) -> tuple[Appointment | None, str | None]:
    """
    Persist patient + appointment from PublicBookingSerializer.validated_data.

    Returns (appointment, None) on success, or (None, error_message) on failure
    (slot taken, blocked interval, invalid provider/service combo).
    """
    phone_normalized = normalize_phone(validated["phone"])
    patient, _ = Patient.objects.update_or_create(
        phone=phone_normalized,
        defaults={
            "first_name": validated["first_name"],
            "last_name": validated["last_name"],
            "email": (validated.get("email") or "").strip(),
        },
    )

    if validated.get("service_id"):
        try:
            service = Service.objects.get(pk=validated["service_id"])
        except Service.DoesNotExist:
            return None, "That service is not available for online booking."
        if not service.is_active or not service.show_in_public_booking:
            return None, "That service is not available for online booking."
    else:
        service, _ = Service.objects.get_or_create(
            name=validated["service_name"],
            defaults={
                "description": "Created from public booking flow",
                "duration_minutes": validated["service_duration_minutes"],
                "price": validated["service_price"],
                "billing_code": "",
                "is_active": True,
                "show_in_public_booking": True,
            },
        )
        if service.duration_minutes != validated["service_duration_minutes"] or service.price != validated["service_price"]:
            service.duration_minutes = validated["service_duration_minutes"]
            service.price = validated["service_price"]
            service.save(update_fields=["duration_minutes", "price", "updated_at"])

    if validated.get("provider_id"):
        provider = Provider.objects.get(pk=validated["provider_id"])
        if not provider.services.filter(pk=service.id).exists() and provider.services.exists():
            return None, "This provider does not offer the selected service."
    else:
        User = get_user_model()
        provider_name = validated.get("provider_name") or "Unknown"
        provider_slug = slugify(provider_name) or "provider"
        username = f"{provider_slug}_doctor"
        user, _ = User.objects.get_or_create(
            username=username,
            defaults={
                "full_name": provider_name,
                "email": f"{provider_slug}@reliefchiropractic.local",
                "role": "doctor",
            },
        )
        provider, _ = Provider.objects.get_or_create(
            user=user,
            defaults={"title": "Doctor", "specialty": "Chiropractic", "active": True},
        )

    start_dt = timezone.datetime.combine(validated["appointment_date"], validated["start_time"])
    end_dt = start_dt + timezone.timedelta(minutes=service.duration_minutes)
    start_time = start_dt.time()
    end_time = end_dt.time()

    if provider_interval_blocked_online(provider.pk, validated["appointment_date"], start_time, end_time):
        return None, "That time is not open for online booking with this provider. Please pick another slot."

    lapse_msg = chiropractic_booking_must_use_intake(patient, service)
    if lapse_msg:
        return None, lapse_msg

    overlapping = (
        Appointment.objects.filter(
            provider=provider,
            appointment_date=validated["appointment_date"],
            start_time__lt=end_time,
            end_time__gt=start_time,
        )
        .exclude(status__in=[Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW, Appointment.Status.COMPLETED])
        .exists()
    )
    if overlapping:
        return None, "That time slot is no longer available. Please choose another time."

    appointment = Appointment.objects.create(
        patient=patient,
        provider=provider,
        booked_service=service,
        appointment_date=validated["appointment_date"],
        start_time=start_time,
        end_time=end_time,
        status=Appointment.Status.BOOKED,
    )

    def queue_after_book():
        import logging as _log

        from apps.notifications.tasks import (
            notify_provider_new_booking_task,
            send_booking_confirmation_email_task,
            send_booking_confirmation_sms_task,
            sync_appointment_google_calendar_task,
        )

        _logger = _log.getLogger(__name__)
        tasks = [
            ("sms", send_booking_confirmation_sms_task),
            ("email", send_booking_confirmation_email_task),
            ("provider_notify", notify_provider_new_booking_task),
            ("gcal", sync_appointment_google_calendar_task),
        ]
        for label, task_fn in tasks:
            try:
                task_fn.delay(appointment.id)
            except Exception:
                _logger.warning(
                    "Celery dispatch failed for %s (appt %s), running synchronously",
                    label, appointment.id,
                )
                try:
                    task_fn(appointment.id)
                except Exception:
                    _logger.exception("Sync fallback also failed for %s (appt %s)", label, appointment.id)

    def queue_in_app():
        from apps.clinic.in_app_notify import create_new_booking_in_app_notification

        create_new_booking_in_app_notification(appointment.id)

    transaction.on_commit(queue_after_book)
    transaction.on_commit(queue_in_app)

    return appointment, None


def reschedule_appointment_public(
    *,
    phone_normalized: str,
    appointment_id: int,
    new_date,
    new_start,
) -> tuple[Appointment | None, str | None]:
    """
    Move an existing BOOKED visit to a new open slot. Verifies the patient's phone matches.
    Same online-booking rules as new appointments (blocks, overlaps). Does not re-run intake policy.
    """
    appt = (
        Appointment.objects.select_related("patient", "provider", "booked_service")
        .filter(pk=appointment_id)
        .first()
    )
    if not appt:
        return None, "We could not find that appointment."

    patient = appt.patient
    phone_ok = normalize_phone(patient.phone) == phone_normalized
    if not phone_ok:
        for p in Patient.objects.all():
            if p.pk == patient.pk and normalize_phone(p.phone) == phone_normalized:
                phone_ok = True
                break
    if not phone_ok:
        return None, "That phone number does not match this appointment. Please call the clinic for help."

    if appt.status != Appointment.Status.BOOKED:
        return None, "Only upcoming scheduled visits can be rescheduled online. Please call the clinic."

    service = appt.booked_service
    if not service or not service.is_active or not service.show_in_public_booking:
        return None, "This visit type cannot be rescheduled online. Please call the clinic."

    provider = appt.provider
    today = timezone.localdate()
    if new_date < today:
        return None, "Pick today or a future date."

    if new_date == today:
        slot_dt = timezone.make_aware(datetime.combine(new_date, new_start))
        if slot_dt <= timezone.now():
            return None, "Pick a time later today that has not passed yet."

    start_dt = datetime.combine(new_date, new_start)
    end_dt = start_dt + timedelta(minutes=service.duration_minutes)
    start_time = start_dt.time()
    end_time = end_dt.time()

    if provider_interval_blocked_online(provider.pk, new_date, start_time, end_time):
        return None, "That time is not open for online booking with this provider. Please pick another slot."

    overlapping = (
        Appointment.objects.filter(
            provider=provider,
            appointment_date=new_date,
            start_time__lt=end_time,
            end_time__gt=start_time,
        )
        .exclude(pk=appt.pk)
        .exclude(
            status__in=[
                Appointment.Status.CANCELLED,
                Appointment.Status.NO_SHOW,
                Appointment.Status.COMPLETED,
            ]
        )
        .exists()
    )
    if overlapping:
        return None, "That time slot is no longer available. Please choose another time."

    old = {
        "appointment_date": appt.appointment_date,
        "start_time": appt.start_time,
        "end_time": appt.end_time,
        "status": appt.status,
        "provider_id": appt.provider_id,
        "booked_service_id": appt.booked_service_id,
    }

    appt.appointment_date = new_date
    appt.start_time = start_time
    appt.end_time = end_time
    appt.sms_reminder_sent_at = None
    appt.save(
        update_fields=["appointment_date", "start_time", "end_time", "sms_reminder_sent_at", "updated_at"]
    )

    aid = appt.id
    change_lines: list[str] = []
    if old["appointment_date"] != appt.appointment_date:
        change_lines.append(f"Date: {old['appointment_date']} → {appt.appointment_date}.")
    if old["start_time"] != appt.start_time or old["end_time"] != appt.end_time:
        change_lines.append(
            f"Time: {format_time_12h(old['start_time'])} → {format_time_12h(appt.start_time)}."
        )
    if old["status"] != appt.status:
        change_lines.append(f"Status: {old['status']} → {appt.status}.")
    if old["booked_service_id"] != appt.booked_service_id:
        change_lines.append("Booked service changed.")

    old_provider_id = None
    old_date_iso = None
    old_time_iso = None
    if old["provider_id"] != appt.provider_id:
        change_lines.append("This appointment is now on your schedule (reassigned).")
        old_provider_id = old["provider_id"]
        old_date_iso = str(old["appointment_date"])
        old_time_iso = old["start_time"].isoformat()

    def queue_calendar():
        from apps.notifications.tasks import sync_appointment_google_calendar_task

        sync_appointment_google_calendar_task.delay(aid)

    def queue_doctor_alerts():
        from apps.notifications.tasks import notify_provider_schedule_change_task

        if change_lines:
            notify_provider_schedule_change_task.delay(
                aid,
                change_lines,
                old_provider_id=old_provider_id,
                old_date_iso=old_date_iso,
                old_time_iso=old_time_iso,
            )

    def queue_in_app():
        from apps.clinic.in_app_notify import create_schedule_change_in_app_notifications

        if change_lines:
            create_schedule_change_in_app_notifications(
                aid,
                change_lines,
                old_provider_id,
                old_date_iso,
                old_time_iso,
            )

    transaction.on_commit(queue_calendar)
    transaction.on_commit(queue_doctor_alerts)
    transaction.on_commit(queue_in_app)

    return appt, None


def cancel_appointment_public(*, phone_normalized: str, appointment_id: int) -> tuple[Appointment | None, str | None]:
    """
    Patient cancels before visit start (online). Chiropractic: no fee. Massage: full fee if under 24h notice.
    """
    appt = (
        Appointment.objects.select_related("patient", "provider", "booked_service")
        .filter(pk=appointment_id)
        .first()
    )
    if not appt:
        return None, "We could not find that appointment."

    patient = appt.patient
    phone_ok = normalize_phone(patient.phone) == phone_normalized
    if not phone_ok:
        for p in Patient.objects.all():
            if p.pk == patient.pk and normalize_phone(p.phone) == phone_normalized:
                phone_ok = True
                break
    if not phone_ok:
        return None, "That phone number does not match this appointment. Please call the clinic for help."

    if appt.status != Appointment.Status.BOOKED:
        return None, "This visit can no longer be cancelled online. Please call the clinic."

    svc = appt.booked_service
    if not svc or not svc.is_active or not svc.show_in_public_booking:
        return None, "This visit cannot be cancelled online. Please call the clinic."

    if svc.service_type not in (
        Service.ServiceType.CHIROPRACTIC,
        Service.ServiceType.MASSAGE,
    ):
        return None, "This visit type cannot be cancelled online. Please call the clinic."

    now = timezone.now()
    start_dt = timezone.make_aware(datetime.combine(appt.appointment_date, appt.start_time))
    if now >= start_dt:
        return None, "You can only cancel online before your appointment start time. Please call the clinic."

    notice = start_dt - now
    apply_late_massage_fee = (
        svc.service_type == Service.ServiceType.MASSAGE and notice < timedelta(hours=24)
    )

    try:
        with transaction.atomic():
            locked = (
                Appointment.objects.select_for_update()
                .select_related("patient", "booked_service", "provider")
                .get(pk=appt.id)
            )
            if locked.status != Appointment.Status.BOOKED:
                return None, "This visit can no longer be cancelled online. Please call the clinic."
            svc_locked = locked.booked_service
            if apply_late_massage_fee and svc_locked:
                fee = svc_locked.price or Decimal("0")
                if fee > 0:
                    from .no_show_billing import apply_late_cancel_fee_for_appointment

                    apply_late_cancel_fee_for_appointment(locked, fee)
            locked.status = Appointment.Status.CANCELLED
            locked.checked_in_at = None
            locked.consultation_started_at = None
            locked.completed_at = None
            locked.save(
                update_fields=[
                    "status",
                    "checked_in_at",
                    "consultation_started_at",
                    "completed_at",
                    "updated_at",
                ]
            )
    except RestValidationError as exc:
        detail = exc.detail
        if isinstance(detail, dict) and "detail" in detail:
            inner = detail["detail"]
            if isinstance(inner, list) and inner:
                return None, str(inner[0])
            return None, inner if isinstance(inner, str) else str(inner)
        return None, str(detail)

    aid = locked.id

    def queue_calendar():
        from apps.notifications.tasks import sync_appointment_google_calendar_task

        sync_appointment_google_calendar_task.delay(aid)

    transaction.on_commit(queue_calendar)

    return locked, None
