"""
Shared logic for creating an appointment from the public booking payload.

Used by the REST `book` action and by the Twilio voice assistant webhook.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from .booking_availability import provider_interval_blocked_online
from .models import Appointment, Patient, Provider, Service
from .utils import normalize_phone


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
            "email": validated.get("email", ""),
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
