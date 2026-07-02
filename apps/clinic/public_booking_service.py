"""
Shared logic for creating an appointment from the public booking payload.

Used by the REST `book` action and by the Twilio voice assistant webhook.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, OperationalError, transaction
from django.utils import timezone
from django.utils.text import slugify
from rest_framework.exceptions import ValidationError as RestValidationError

from .booking_availability import provider_interval_blocked_online
from .booking_provider_eligibility import provider_can_offer_service_online
from .chiropractic_booking_policy import chiropractic_booking_must_use_intake
from .online_booking_hours import (
    PUBLIC_BOOKING_HOURS_BLURB,
    interval_outside_effective_public_window,
    public_booking_treatment_duration_minutes,
)
from .models import Appointment, Patient, Provider, Service, Visit
from .patient_phone import (
    get_or_create_patient_for_public_booking,
    patient_matches_phone_normalized,
    patients_matching_phone,
)
from .utils import format_time_12h, normalize_phone

logger = logging.getLogger(__name__)

# Post-massage turnover reserved on the provider calendar for public online booking (not billed time).
MASSAGE_PUBLIC_BOOKING_BUFFER_AFTER_MINUTES = 15


def _appointment_start_aware_in_clinic_tz(appt: Appointment) -> datetime:
    """
    Appointment start as an aware datetime in CLINIC_TIMEZONE (e.g. America/Detroit).

    Used instead of ``make_aware(combine(...))`` without tzinfo, which treated slots as UTC.
    """
    from apps.clinic.clinic_time import aware_appointment_start

    return aware_appointment_start(appt.appointment_date, appt.start_time)


def _local_now_passed_appointment_start(appt: Appointment) -> bool:
    """
    True when clinic-local clock is already at or past the appointment start (date + time).

    Cheap guard when we only need a wall-clock comparison (e.g. my-appointments list).
    """
    from apps.clinic.clinic_time import slot_start_is_in_past

    return slot_start_is_in_past(appt.appointment_date, appt.start_time)


def normalize_caller_phone(phone_raw: str) -> str | None:
    """Normalize caller ID / spoken phone to E.164; None when missing or invalid."""
    try:
        norm = normalize_phone((phone_raw or "").strip())
    except Exception:
        logger.warning("normalize_phone failed for caller phone", exc_info=True)
        return None
    return norm or None


def _self_service_empty_hint(*, patient_ids: list[int], today) -> str:
    """Why no online-manageable BOOKED visits were found (voice + web parity)."""
    future = (
        Appointment.objects.filter(
            patient_id__in=patient_ids,
            appointment_date__gte=today,
        )
        .exclude(
            status__in=[
                Appointment.Status.CANCELLED,
                Appointment.Status.NO_SHOW,
                Appointment.Status.COMPLETED,
            ]
        )
        .select_related("booked_service")
    )
    if not future.exists():
        if Appointment.objects.filter(
            patient_id__in=patient_ids,
            appointment_date__lt=today,
        ).exclude(status=Appointment.Status.CANCELLED).exists():
            return (
                "We found older visits on this number but nothing scheduled from today onward. "
                "If you expected a future visit, call the clinic — the number on file may differ."
            )
        return (
            "We don't see any scheduled visits on or after today for this number. "
            "Use the same cell number from when you booked, or call the clinic."
        )

    active = future.filter(
        status__in=[
            Appointment.Status.CHECKED_IN,
            Appointment.Status.IN_CONSULTATION,
            Appointment.Status.AWAITING_PAYMENT,
        ]
    )
    if active.exists():
        return (
            "You have a visit today that is already checked in or in progress. "
            "Only visits still marked Booked can be changed online — call the front desk for help."
        )

    booked = future.filter(status=Appointment.Status.BOOKED)
    for a in booked:
        if a.appointment_date == today and _local_now_passed_appointment_start(a):
            return (
                "Today's visit time has already started or passed for online changes. "
                "Call the clinic if you still need to reschedule or cancel."
            )
        svc = a.booked_service
        if not svc or not svc.is_active or not svc.show_in_public_booking:
            return (
                "A visit is on file for this number but cannot be changed online. "
                "Please call the clinic for help."
            )

    return (
        "No upcoming visits found for this number that can be changed online. "
        "Please call the clinic for help."
    )


def public_self_service_upcoming_appointments(
    phone_raw: str,
    *,
    limit: int = 5,
) -> tuple[list[Appointment], str | None]:
    """
    Booked visits a caller may cancel/reschedule online (matches web my-appointments manage mode).
    Skips today's visits whose start time has already passed.
    """
    from apps.clinic.clinic_time import clinic_localdate
    from apps.clinic.patient_phone import patients_matching_phone

    norm = normalize_caller_phone(phone_raw)
    if not norm:
        return [], "We couldn't read the phone number on this call. Please call the clinic for help."

    patients = patients_matching_phone(norm)
    if not patients:
        return [], (
            "We don't see any patient profile for this phone number. "
            "Use the same cell number from when you booked, or call the clinic."
        )

    patient_ids = [p.id for p in patients]
    today = clinic_localdate()
    rows = (
        Appointment.objects.filter(
            patient_id__in=patient_ids,
            appointment_date__gte=today,
            status=Appointment.Status.BOOKED,
        )
        .select_related("patient", "provider", "booked_service")
        .order_by("appointment_date", "start_time")
    )
    out: list[Appointment] = []
    for appt in rows:
        if appt.appointment_date == today and _local_now_passed_appointment_start(appt):
            continue
        out.append(appt)
        if len(out) >= limit:
            break

    if out:
        return out, None
    return [], _self_service_empty_hint(patient_ids=patient_ids, today=today)


def public_self_service_household_context(phone_raw: str) -> dict:
    """Profiles sharing this phone (parent/child households)."""
    norm = normalize_caller_phone(phone_raw)
    patients = patients_matching_phone(norm) if norm else []
    return {
        "ambiguous_phone": len(patients) > 1,
        "household_members": [
            {"first_name": p.first_name, "last_name": p.last_name}
            for p in patients
        ],
    }


def appointment_to_self_service_payload(appt: Appointment) -> dict:
    svc = appt.booked_service
    svc_name = svc.label_for_public_booking() if svc else "appointment"
    patient = appt.patient
    patient_name = ""
    if patient:
        patient_name = f"{patient.first_name} {patient.last_name}".strip()
    return {
        "appointment_id": appt.id,
        "patient_first_name": patient.first_name if patient else "",
        "patient_last_name": patient.last_name if patient else "",
        "patient_name": patient_name,
        "service": svc_name,
        "service_id": svc.id if svc else None,
        "date": appt.appointment_date.isoformat(),
        "time": format_time_12h(appt.start_time),
        "provider_id": appt.provider_id,
        "can_reschedule_online": bool(svc and svc.is_active),
    }


def public_online_booking_calendar_span_minutes(service: Service) -> int:
    """Minutes [start, end) blocked on the provider schedule for overlap checks and slot listing."""
    n = int(service.duration_minutes)
    if service.service_type == Service.ServiceType.MASSAGE:
        return n + MASSAGE_PUBLIC_BOOKING_BUFFER_AFTER_MINUTES
    return n


def apply_patient_sms_consent_from_booking(patient: Patient, *, consented: bool) -> None:
    """Persist SMS reminder consent from public booking / reschedule (opt-in or opt-out)."""
    if consented:
        patient.sms_consent = True
        patient.sms_consent_at = timezone.now()
    else:
        patient.sms_consent = False
        patient.sms_consent_at = None
    patient.save(update_fields=["sms_consent", "sms_consent_at", "updated_at"])


def record_patient_sms_consent_from_booking(patient: Patient) -> None:
    """Backward-compatible opt-in helper."""
    apply_patient_sms_consent_from_booking(patient, consented=True)


def create_appointment_from_public_booking(validated: dict) -> tuple[Appointment | None, str | None]:
    """
    Persist patient + appointment from PublicBookingSerializer.validated_data.

    Returns (appointment, None) on success, or (None, error_message) on failure
    (slot taken, blocked interval, invalid provider/service combo).
    """
    phone_normalized = normalize_phone(validated["phone"])
    patient = get_or_create_patient_for_public_booking(
        phone_normalized=phone_normalized,
        first_name=validated["first_name"],
        last_name=validated["last_name"],
        email=(validated.get("email") or "").strip(),
    )
    apply_patient_sms_consent_from_booking(patient, consented=bool(validated.get("sms_consent", True)))

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
        if not provider_can_offer_service_online(provider, service):
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
    span = public_online_booking_calendar_span_minutes(service)
    end_dt = start_dt + timezone.timedelta(minutes=span)
    start_time = start_dt.time()
    end_time = end_dt.time()
    treat_end_dt = start_dt + timezone.timedelta(minutes=public_booking_treatment_duration_minutes(service))
    treatment_end_time = treat_end_dt.time()

    if interval_outside_effective_public_window(validated["appointment_date"], start_time, end_time, service):
        return None, PUBLIC_BOOKING_HOURS_BLURB

    if provider_interval_blocked_online(
        provider.pk,
        validated["appointment_date"],
        start_time,
        end_time,
        block_overlap_end=treatment_end_time,
    ):
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

    reason_for_visit = (validated.get("reason_for_visit") or "").strip()
    if reason_for_visit:
        Visit.objects.get_or_create(
            appointment=appointment,
            defaults={
                "patient": patient,
                "provider": provider,
                "status": Visit.Status.OPEN,
                "reason_for_visit": reason_for_visit,
            },
        )

    def queue_after_book():
        from apps.clinic.patient_appointment_notifications import queue_patient_booking_confirmations

        queue_patient_booking_confirmations(
            appointment.id,
            include_provider_notify=True,
            include_gcal=True,
        )

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
    sms_consent: bool = True,
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
    if not patient_matches_phone_normalized(patient, phone_normalized):
        return None, "That phone number does not match this appointment. Please call the clinic for help."

    if appt.status != Appointment.Status.BOOKED:
        return None, "Only upcoming scheduled visits can be rescheduled online. Please call the clinic."

    service = appt.booked_service
    if not service or not service.is_active:
        return None, "This visit type cannot be rescheduled online. Please call the clinic."

    from apps.clinic.clinic_time import clinic_localdate, slot_start_is_in_past

    provider = appt.provider
    today = clinic_localdate()
    if new_date < today:
        return None, "Pick today or a future date."

    if slot_start_is_in_past(new_date, new_start):
        return None, "Pick a time later today that has not passed yet."

    start_dt = datetime.combine(new_date, new_start)
    span = public_online_booking_calendar_span_minutes(service)
    end_dt = start_dt + timedelta(minutes=span)
    start_time = start_dt.time()
    end_time = end_dt.time()
    treat_end_dt = start_dt + timedelta(minutes=public_booking_treatment_duration_minutes(service))
    treatment_end_time = treat_end_dt.time()

    if interval_outside_effective_public_window(new_date, start_time, end_time, service):
        return None, PUBLIC_BOOKING_HOURS_BLURB

    if provider_interval_blocked_online(
        provider.pk, new_date, start_time, end_time, block_overlap_end=treatment_end_time
    ):
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
    appt.clear_reminder_timestamps()
    appt.save(
        update_fields=[
            "appointment_date",
            "start_time",
            "end_time",
            "day_before_reminder_sms_at",
            "day_before_reminder_email_at",
            "same_day_reminder_sms_at",
            "same_day_reminder_email_at",
            "late_checkin_sms_at",
            "updated_at",
        ]
    )

    apply_patient_sms_consent_from_booking(patient, consented=sms_consent)

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

    def queue_patient_confirmations():
        from apps.clinic.patient_appointment_notifications import queue_patient_reschedule_confirmations

        queue_patient_reschedule_confirmations(aid, staff_initiated=False)

    transaction.on_commit(queue_patient_confirmations)

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
    if not patient_matches_phone_normalized(patient, phone_normalized):
        return None, "That phone number does not match this appointment. Please call the clinic for help."

    if appt.status != Appointment.Status.BOOKED:
        return None, "This visit can no longer be cancelled online. Please call the clinic."

    svc = appt.booked_service
    # Allow cancellation for already-booked visits even if the service was later hidden/inactivated.
    # Restricting by current service visibility blocks legitimate patient self-service cancels.

    try:
        start_dt = _appointment_start_aware_in_clinic_tz(appt)
    except Exception:
        logger.exception(
            "Could not compute appointment start time for cancellation appointment_id=%s",
            appt.id,
        )
        return None, "We could not process this cancellation online. Please call the clinic."

    now = timezone.now()
    if now >= start_dt:
        return None, "You can only cancel online before your appointment start time. Please call the clinic."

    notice = start_dt - now
    apply_late_massage_fee = bool(
        svc and svc.service_type == Service.ServiceType.MASSAGE and notice < timedelta(hours=24)
    )

    locked: Appointment | None = None
    try:
        with transaction.atomic():
            try:
                # booked_service is nullable → select_related + FOR UPDATE becomes an outer join on Postgres,
                # which raises "FOR UPDATE cannot be applied to the nullable side of an outer join".
                # Lock only appointment rows; related rows are still readable in the same transaction.
                locked = (
                    Appointment.objects.select_for_update(of=("self",))
                    .select_related("patient", "booked_service", "provider")
                    .get(pk=appt.id)
                )
            except Appointment.DoesNotExist:
                return None, "We could not find that appointment."

            if locked.status != Appointment.Status.BOOKED:
                return None, "This visit can no longer be cancelled online. Please call the clinic."
            svc_locked = locked.booked_service
            if apply_late_massage_fee and svc_locked:
                fee = svc_locked.price or Decimal("0")
                if fee > 0:
                    try:
                        from .no_show_billing import apply_late_cancel_fee_for_appointment

                        apply_late_cancel_fee_for_appointment(locked, fee)
                    except Exception:
                        logger.exception(
                            "Late cancel fee could not be applied; continuing with cancellation appointment_id=%s",
                            locked.id,
                        )
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
    except IntegrityError:
        logger.exception(
            "cancel_appointment_public IntegrityError (likely billing row conflict) appointment_id=%s",
            appointment_id,
        )
        return None, (
            "This visit could not be cancelled online because billing data on file conflicts with "
            "cancellation (for example an open invoice). Please call the clinic so we can cancel it for you."
        )
    except OperationalError:
        logger.exception(
            "cancel_appointment_public database OperationalError appointment_id=%s",
            appointment_id,
        )
        return None, (
            "The server could not reach the database just now. Please try again in a minute, or call the clinic."
        )
    except Exception:
        logger.exception(
            "cancel_appointment_public failed during transaction appointment_id=%s",
            appointment_id,
        )
        return None, "We could not complete this cancellation online. Please call the clinic."

    if locked is None:
        logger.error(
            "cancel_appointment_public finished transaction but locked is None appointment_id=%s",
            appointment_id,
        )
        return None, "We could not complete this cancellation online. Please call the clinic."
    aid = locked.id

    def queue_calendar():
        try:
            from apps.notifications.tasks import sync_appointment_google_calendar_task

            sync_appointment_google_calendar_task.delay(aid)
        except Exception:
            logger.exception(
                "Could not queue Google Calendar sync after cancellation appointment_id=%s",
                aid,
            )

    transaction.on_commit(queue_calendar)

    def queue_patient_confirmations():
        from apps.clinic.patient_appointment_notifications import queue_patient_cancel_confirmations

        queue_patient_cancel_confirmations(aid, staff_initiated=False)

    transaction.on_commit(queue_patient_confirmations)

    return locked, None
