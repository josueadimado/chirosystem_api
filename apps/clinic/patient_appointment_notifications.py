"""
Queue patient SMS/email for booking, cancel, and reschedule.

Public self-service uses TCPA sms_consent on cancel/reschedule SMS.
Staff/doctor actions use communication prefs only (same as booking confirmation).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

TaskSpec = tuple[str, Callable[..., Any], dict[str, Any]]


def _dispatch_patient_notification_tasks(
    specs: list[TaskSpec],
    appointment_id: int,
    *,
    context: str,
) -> None:
    """Celery delay with synchronous fallback; failures are logged only."""
    for label, task_fn, kwargs in specs:
        try:
            task_fn.delay(appointment_id, **kwargs)
        except Exception:
            logger.warning(
                "Celery dispatch failed for patient %s %s (appt %s), running synchronously",
                context,
                label,
                appointment_id,
            )
            try:
                task_fn(appointment_id, **kwargs)
            except Exception:
                logger.exception(
                    "Sync fallback failed for patient %s %s (appt %s)",
                    context,
                    label,
                    appointment_id,
                )


def queue_patient_booking_confirmations(
    appointment_id: int,
    *,
    include_provider_notify: bool = False,
    include_gcal: bool = False,
) -> None:
    """New appointment booked — same messages as public online booking."""
    from apps.notifications.tasks import (
        notify_provider_new_booking_task,
        send_booking_confirmation_email_task,
        send_booking_confirmation_sms_task,
        sync_appointment_google_calendar_task,
    )

    specs: list[TaskSpec] = [
        ("sms", send_booking_confirmation_sms_task, {}),
        ("email", send_booking_confirmation_email_task, {}),
    ]
    if include_provider_notify:
        specs.append(("provider_notify", notify_provider_new_booking_task, {}))
    if include_gcal:
        specs.append(("gcal", sync_appointment_google_calendar_task, {}))
    _dispatch_patient_notification_tasks(specs, appointment_id, context="booking")


def queue_patient_cancel_confirmations(
    appointment_id: int,
    *,
    staff_initiated: bool = True,
) -> None:
    """Cancel confirmation SMS/email (staff portal skips sms_consent check)."""
    from apps.notifications.tasks import (
        send_patient_cancel_confirmation_email_task,
        send_patient_cancel_confirmation_sms_task,
    )

    require_sms_consent = not staff_initiated
    specs: list[TaskSpec] = [
        (
            "sms",
            send_patient_cancel_confirmation_sms_task,
            {"require_sms_consent": require_sms_consent},
        ),
        ("email", send_patient_cancel_confirmation_email_task, {}),
    ]
    _dispatch_patient_notification_tasks(specs, appointment_id, context="cancel")


def queue_patient_reschedule_confirmations(
    appointment_id: int,
    *,
    staff_initiated: bool = True,
) -> None:
    """Reschedule confirmation after date/time change."""
    if staff_initiated:
        from apps.notifications.tasks import (
            send_provider_dashboard_reschedule_patient_email_task,
            send_provider_dashboard_reschedule_patient_sms_task,
        )

        specs: list[TaskSpec] = [
            ("sms", send_provider_dashboard_reschedule_patient_sms_task, {}),
            ("email", send_provider_dashboard_reschedule_patient_email_task, {}),
        ]
    else:
        from apps.notifications.tasks import (
            send_patient_reschedule_confirmation_email_task,
            send_patient_reschedule_confirmation_sms_task,
        )

        specs = [
            (
                "sms",
                send_patient_reschedule_confirmation_sms_task,
                {"require_sms_consent": True},
            ),
            ("email", send_patient_reschedule_confirmation_email_task, {}),
        ]
    _dispatch_patient_notification_tasks(specs, appointment_id, context="reschedule")
