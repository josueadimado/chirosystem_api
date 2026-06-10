"""
Generate dates and book recurring public appointments (weekly / biweekly / monthly).
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from django.db import transaction
from django.utils import timezone

from .booking_provider_eligibility import provider_can_offer_service_online
from .chiropractic_booking_policy import chiropractic_booking_must_use_intake
from .models import Appointment, AppointmentSeries, Patient, Provider, Service, Visit
from .patient_phone import get_or_create_patient_for_public_booking
from .online_booking_hours import public_booking_treatment_duration_minutes
from .public_booking_service import (
    apply_patient_sms_consent_from_booking,
    public_online_booking_calendar_span_minutes,
)
from .utils import format_time_12h, normalize_phone

MAX_RECURRING_OCCURRENCES = 12
MIN_RECURRING_OCCURRENCES = 2


def max_public_booking_date() -> date:
    from apps.clinic.timezone_utils import today_clinic

    today = today_clinic()
    y, m = today.year, today.month + 6
    while m > 12:
        m -= 12
        y += 1
    last_day = calendar.monthrange(y, m)[1]
    d = min(today.day, last_day)
    return date(y, m, d)


def add_months_same_day(d: date, months: int) -> date:
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def generate_recurring_dates(
    *,
    first_date: date,
    recurrence: str,
    occurrence_count: int,
    max_date: date | None = None,
) -> list[date]:
    """Build occurrence dates from first_date; caps at max_date and MAX_RECURRING_OCCURRENCES."""
    cap = max_date or max_public_booking_date()
    count = max(MIN_RECURRING_OCCURRENCES, min(int(occurrence_count), MAX_RECURRING_OCCURRENCES))
    out: list[date] = []
    cur = first_date
    for _ in range(count):
        if cur > cap:
            break
        out.append(cur)
        if recurrence == AppointmentSeries.Recurrence.WEEKLY:
            cur = cur + timedelta(days=7)
        elif recurrence == AppointmentSeries.Recurrence.BIWEEKLY:
            cur = cur + timedelta(days=14)
        elif recurrence == AppointmentSeries.Recurrence.MONTHLY:
            cur = add_months_same_day(cur, 1)
        else:
            cur = cur + timedelta(days=7)
    return out


def _resolve_service_and_provider(data: dict) -> tuple[Service | None, Provider | None, str | None]:
    if not data.get("service_id"):
        return None, None, "service_id is required."

    try:
        service = Service.objects.get(pk=data["service_id"])
    except Service.DoesNotExist:
        return None, None, "That service is not available for online booking."
    if not service.is_active or not service.show_in_public_booking:
        return None, None, "That service is not available for online booking."

    if not data.get("provider_id"):
        return None, None, "provider_id is required."

    try:
        provider = Provider.objects.get(pk=data["provider_id"], active=True)
    except Provider.DoesNotExist:
        return None, None, "Invalid or inactive provider."
    if not provider_can_offer_service_online(provider, service):
        return None, None, "This provider does not offer the selected service."

    return service, provider, None


def validate_public_booking_slot(
    validated: dict,
    *,
    service: Service,
    provider: Provider,
    patient: Patient | None = None,
    skip_intake: bool = False,
) -> str | None:
    """Return an error message if the slot is not bookable; None if OK."""
    from .booking_availability import provider_interval_blocked_online
    from .online_booking_hours import (
        PUBLIC_BOOKING_HOURS_BLURB,
        interval_outside_effective_public_window,
    )

    start_dt = timezone.datetime.combine(validated["appointment_date"], validated["start_time"])
    span = public_online_booking_calendar_span_minutes(service)
    end_dt = start_dt + timezone.timedelta(minutes=span)
    start_time = start_dt.time()
    end_time = end_dt.time()
    treat_end_dt = start_dt + timezone.timedelta(minutes=public_booking_treatment_duration_minutes(service))
    treatment_end_time = treat_end_dt.time()

    if interval_outside_effective_public_window(
        validated["appointment_date"], start_time, end_time, service
    ):
        return PUBLIC_BOOKING_HOURS_BLURB

    if provider_interval_blocked_online(
        provider.pk,
        validated["appointment_date"],
        start_time,
        end_time,
        block_overlap_end=treatment_end_time,
    ):
        return "That time is not open for online booking with this provider. Please pick another slot."

    if not skip_intake and patient:
        lapse_msg = chiropractic_booking_must_use_intake(patient, service)
        if lapse_msg:
            return lapse_msg

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
        return "That time slot is no longer available. Please choose another time."

    return None


def _booking_payload_for_date(
    data: dict,
    *,
    service: Service,
    provider: Provider,
    appt_date: date,
    start_time,
    patient: Patient | None = None,
) -> dict:
    return {
        "first_name": data.get("first_name") or (patient.first_name if patient else ""),
        "last_name": data.get("last_name") or (patient.last_name if patient else ""),
        "phone": data.get("phone") or (patient.phone if patient else ""),
        "email": data.get("email") or (patient.email if patient else ""),
        "sms_consent": data.get("sms_consent", True),
        "service_id": service.id,
        "provider_id": provider.id,
        "service_name": service.label_for_public_booking(),
        "service_duration_minutes": int(service.duration_minutes),
        "service_price": service.price,
        "appointment_date": appt_date,
        "start_time": start_time,
        "reason_for_visit": (data.get("reason_for_visit") or "").strip(),
    }


def preview_recurring_slots(data: dict) -> dict:
    """Return proposed dates with availability status for each (no DB writes)."""
    from apps.clinic.timezone_utils import today_clinic

    service, provider, err = _resolve_service_and_provider(data)
    if err:
        return {"ok": False, "detail": err}

    recurrence = (data.get("recurrence") or "").strip().lower()
    valid_recurrence = {c.value for c in AppointmentSeries.Recurrence}
    if recurrence not in valid_recurrence:
        return {"ok": False, "detail": "recurrence must be weekly, biweekly, or monthly."}

    try:
        occurrence_count = int(data.get("occurrence_count") or MIN_RECURRING_OCCURRENCES)
    except (TypeError, ValueError):
        return {"ok": False, "detail": "occurrence_count must be a number."}

    first_date = data.get("appointment_date")
    start_time = data.get("start_time")
    if not first_date or not start_time:
        return {"ok": False, "detail": "appointment_date and start_time are required."}

    today = today_clinic()
    if first_date < today:
        return {"ok": False, "detail": "The first visit cannot be in the past."}
    if first_date.weekday() >= 5:
        return {"ok": False, "detail": "Online booking is available Monday through Friday only."}

    dates = generate_recurring_dates(
        first_date=first_date,
        recurrence=recurrence,
        occurrence_count=occurrence_count,
    )
    if len(dates) < MIN_RECURRING_OCCURRENCES:
        return {
            "ok": False,
            "detail": "Not enough dates fit within the booking window. Try fewer visits or an earlier start date.",
        }

    patient = None
    phone = (data.get("phone") or "").strip()
    if phone:
        from apps.clinic.patient_phone import patients_matching_phone

        matches = patients_matching_phone(normalize_phone(phone))
        if matches:
            patient = matches[0]

    occurrences = []
    all_available = True
    for i, appt_date in enumerate(dates):
        if appt_date.weekday() >= 5:
            occurrences.append(
                {
                    "appointment_date": str(appt_date),
                    "start_time_display": format_time_12h(start_time),
                    "status": "weekend",
                    "detail": "Clinic is closed on weekends.",
                }
            )
            all_available = False
            continue

        payload = _booking_payload_for_date(
            data,
            service=service,
            provider=provider,
            appt_date=appt_date,
            start_time=start_time,
            patient=patient,
        )
        slot_err = validate_public_booking_slot(
            payload,
            service=service,
            provider=provider,
            patient=patient,
            skip_intake=i > 0,
        )
        if slot_err:
            status = "intake_required" if "intake" in slot_err.lower() or "new patient" in slot_err.lower() else "unavailable"
            occurrences.append(
                {
                    "appointment_date": str(appt_date),
                    "start_time_display": format_time_12h(start_time),
                    "status": status,
                    "detail": slot_err,
                }
            )
            all_available = False
        else:
            occurrences.append(
                {
                    "appointment_date": str(appt_date),
                    "start_time_display": format_time_12h(start_time),
                    "status": "available",
                    "detail": "",
                }
            )

    return {
        "ok": True,
        "recurrence": recurrence,
        "occurrence_count": len(dates),
        "all_available": all_available,
        "service_name": service.label_for_public_booking(),
        "provider_name": str(provider),
        "start_time_display": format_time_12h(start_time),
        "occurrences": occurrences,
    }


def book_recurring_from_public(data: dict) -> tuple[list[Appointment] | None, str | None]:
    """Create series + all appointments; one combined patient confirmation."""
    preview = preview_recurring_slots(data)
    if not preview.get("ok"):
        return None, preview.get("detail") or "Could not preview recurring booking."
    if not preview.get("all_available"):
        return None, (
            "One or more visits are not available. Review the list and choose different times or fewer visits."
        )

    service, provider, err = _resolve_service_and_provider(data)
    if err:
        return None, err

    recurrence = (data.get("recurrence") or "").strip().lower()
    first_date = data["appointment_date"]
    start_time = data["start_time"]
    occurrence_count = int(data.get("occurrence_count") or preview["occurrence_count"])
    dates = generate_recurring_dates(
        first_date=first_date,
        recurrence=recurrence,
        occurrence_count=occurrence_count,
    )

    phone_normalized = normalize_phone(data["phone"])
    patient = get_or_create_patient_for_public_booking(
        phone_normalized=phone_normalized,
        first_name=data["first_name"],
        last_name=data["last_name"],
        email=(data.get("email") or "").strip(),
    )
    apply_patient_sms_consent_from_booking(patient, consented=bool(data.get("sms_consent", True)))

    created: list[Appointment] = []
    series: AppointmentSeries | None = None
    appt_ids: list[int] = []

    with transaction.atomic():
        series = AppointmentSeries.objects.create(
            patient=patient,
            provider=provider,
            booked_service=service,
            start_time=start_time,
            recurrence=recurrence,
            first_appointment_date=dates[0],
            last_appointment_date=dates[-1],
            occurrence_count=len(dates),
        )

        reason = (data.get("reason_for_visit") or "").strip()
        for i, appt_date in enumerate(dates):
            payload = _booking_payload_for_date(
                data,
                service=service,
                provider=provider,
                appt_date=appt_date,
                start_time=start_time,
                patient=patient,
            )
            appt, err = _create_appointment_for_series(
                payload,
                patient=patient,
                service=service,
                provider=provider,
                series=series,
                reason_for_visit=reason if i == 0 else "",
                skip_intake_check=i > 0,
            )
            if err:
                transaction.set_rollback(True)
                return None, err
            created.append(appt)
            appt_ids.append(appt.id)

        series_id = series.id

        def queue_series_confirm():
            from apps.clinic.patient_appointment_notifications import queue_series_booking_confirmations

            queue_series_booking_confirmations(series_id, appt_ids)

        def queue_per_appt():
            from apps.clinic.in_app_notify import create_new_booking_in_app_notification
            from apps.notifications.tasks import notify_provider_new_booking_task, sync_appointment_google_calendar_task

            for aid in appt_ids:
                transaction.on_commit(lambda a=aid: notify_provider_new_booking_task.delay(a))
                transaction.on_commit(lambda a=aid: sync_appointment_google_calendar_task.delay(a))
                transaction.on_commit(lambda a=aid: create_new_booking_in_app_notification(a))

        transaction.on_commit(queue_series_confirm)
        transaction.on_commit(queue_per_appt)

    return created, None


def validate_desk_booking_slot(
    *,
    provider: Provider,
    service: Service,
    appt_date: date,
    start_time,
    patient: Patient | None = None,
    skip_intake: bool = False,
) -> str | None:
    """Desk/staff slot check (later hours than public online booking)."""
    from .provider_self_schedule import validate_slot_for_desk_booking_rules

    if not skip_intake and patient:
        lapse_msg = chiropractic_booking_must_use_intake(patient, service)
        if lapse_msg:
            return lapse_msg

    err, _is_conflict = validate_slot_for_desk_booking_rules(
        provider=provider,
        service=service,
        appt_date=appt_date,
        start_time=start_time,
        exclude_appointment_id=None,
    )
    return err


def preview_recurring_slots_desk(data: dict) -> dict:
    """Preview recurring desk bookings (existing patient, desk hour rules)."""
    from apps.clinic.timezone_utils import today_clinic

    try:
        patient_id = int(data["patient_id"])
    except (TypeError, ValueError):
        return {"ok": False, "detail": "patient_id is required."}

    patient = Patient.objects.filter(pk=patient_id).first()
    if not patient:
        return {"ok": False, "detail": "Patient not found."}

    service, provider, err = _resolve_service_and_provider(data)
    if err:
        return {"ok": False, "detail": err}

    recurrence = (data.get("recurrence") or "").strip().lower()
    valid_recurrence = {c.value for c in AppointmentSeries.Recurrence}
    if recurrence not in valid_recurrence:
        return {"ok": False, "detail": "recurrence must be weekly, biweekly, or monthly."}

    try:
        occurrence_count = int(data.get("occurrence_count") or MIN_RECURRING_OCCURRENCES)
    except (TypeError, ValueError):
        return {"ok": False, "detail": "occurrence_count must be a number."}

    first_date = data.get("appointment_date")
    start_time = data.get("start_time")
    if not first_date or not start_time:
        return {"ok": False, "detail": "appointment_date and start_time are required."}

    today = today_clinic()
    if first_date < today:
        return {"ok": False, "detail": "The first visit cannot be in the past."}

    dates = generate_recurring_dates(
        first_date=first_date,
        recurrence=recurrence,
        occurrence_count=occurrence_count,
    )
    if len(dates) < MIN_RECURRING_OCCURRENCES:
        return {
            "ok": False,
            "detail": "Not enough dates fit within the booking window. Try fewer visits or an earlier start date.",
        }

    occurrences = []
    all_available = True
    for i, appt_date in enumerate(dates):
        slot_err = validate_desk_booking_slot(
            provider=provider,
            service=service,
            appt_date=appt_date,
            start_time=start_time,
            patient=patient,
            skip_intake=i > 0,
        )
        if slot_err:
            status = (
                "intake_required"
                if "intake" in slot_err.lower() or "new patient" in slot_err.lower()
                else "unavailable"
            )
            occurrences.append(
                {
                    "appointment_date": str(appt_date),
                    "start_time_display": format_time_12h(start_time),
                    "status": status,
                    "detail": slot_err,
                }
            )
            all_available = False
        else:
            occurrences.append(
                {
                    "appointment_date": str(appt_date),
                    "start_time_display": format_time_12h(start_time),
                    "status": "available",
                    "detail": "",
                }
            )

    return {
        "ok": True,
        "recurrence": recurrence,
        "occurrence_count": len(dates),
        "all_available": all_available,
        "service_name": service.label_for_public_booking(),
        "provider_name": str(provider),
        "start_time_display": format_time_12h(start_time),
        "occurrences": occurrences,
    }


def book_recurring_from_desk(data: dict) -> tuple[list[Appointment] | None, str | None]:
    """Staff desk: book recurring series for an existing patient."""
    preview = preview_recurring_slots_desk(data)
    if not preview.get("ok"):
        return None, preview.get("detail") or "Could not preview recurring booking."
    if not preview.get("all_available"):
        return None, (
            "One or more visits are not available. Review the list and choose different times or fewer visits."
        )

    try:
        patient_id = int(data["patient_id"])
    except (TypeError, ValueError):
        return None, "patient_id is required."

    patient = Patient.objects.filter(pk=patient_id).first()
    if not patient:
        return None, "Patient not found."

    service, provider, err = _resolve_service_and_provider(data)
    if err:
        return None, err

    recurrence = (data.get("recurrence") or "").strip().lower()
    first_date = data["appointment_date"]
    start_time = data["start_time"]
    occurrence_count = int(data.get("occurrence_count") or preview["occurrence_count"])
    dates = generate_recurring_dates(
        first_date=first_date,
        recurrence=recurrence,
        occurrence_count=occurrence_count,
    )

    created: list[Appointment] = []
    appt_ids: list[int] = []

    with transaction.atomic():
        series = AppointmentSeries.objects.create(
            patient=patient,
            provider=provider,
            booked_service=service,
            start_time=start_time,
            recurrence=recurrence,
            first_appointment_date=dates[0],
            last_appointment_date=dates[-1],
            occurrence_count=len(dates),
        )

        for i, appt_date in enumerate(dates):
            payload = {
                "appointment_date": appt_date,
                "start_time": start_time,
            }
            appt, err = _create_appointment_for_series(
                payload,
                patient=patient,
                service=service,
                provider=provider,
                series=series,
                reason_for_visit="",
                skip_intake_check=i > 0,
                use_desk_rules=True,
            )
            if err:
                transaction.set_rollback(True)
                return None, err
            created.append(appt)
            appt_ids.append(appt.id)

        series_id = series.id

        def queue_series_confirm():
            from apps.clinic.patient_appointment_notifications import queue_series_booking_confirmations

            queue_series_booking_confirmations(series_id, appt_ids)

        def queue_per_appt():
            from apps.clinic.in_app_notify import create_new_booking_in_app_notification
            from apps.notifications.tasks import notify_provider_new_booking_task, sync_appointment_google_calendar_task

            for aid in appt_ids:
                transaction.on_commit(lambda a=aid: notify_provider_new_booking_task.delay(a))
                transaction.on_commit(lambda a=aid: sync_appointment_google_calendar_task.delay(a))
                transaction.on_commit(lambda a=aid: create_new_booking_in_app_notification(a))

        transaction.on_commit(queue_series_confirm)
        transaction.on_commit(queue_per_appt)

    return created, None


def _create_appointment_for_series(
    validated: dict,
    *,
    patient: Patient,
    service: Service,
    provider: Provider,
    series: AppointmentSeries,
    reason_for_visit: str,
    skip_intake_check: bool,
    use_desk_rules: bool = False,
) -> tuple[Appointment | None, str | None]:
    if use_desk_rules:
        slot_err = validate_desk_booking_slot(
            provider=provider,
            service=service,
            appt_date=validated["appointment_date"],
            start_time=validated["start_time"],
            patient=patient,
            skip_intake=skip_intake_check,
        )
    else:
        slot_err = validate_public_booking_slot(
            validated,
            service=service,
            provider=provider,
            patient=patient,
            skip_intake=skip_intake_check,
        )
    if slot_err:
        return None, slot_err

    start_dt = timezone.datetime.combine(validated["appointment_date"], validated["start_time"])
    span = public_online_booking_calendar_span_minutes(service)
    end_dt = start_dt + timezone.timedelta(minutes=span)
    start_time = start_dt.time()
    end_time = end_dt.time()

    appointment = Appointment.objects.create(
        patient=patient,
        provider=provider,
        booked_service=service,
        series=series,
        appointment_date=validated["appointment_date"],
        start_time=start_time,
        end_time=end_time,
        status=Appointment.Status.BOOKED,
    )

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

    return appointment, None
