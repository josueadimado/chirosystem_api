"""Admin-only helpers for cleaning up patient chart history rows."""

from django.db import transaction
from django.db.models import Sum

from .models import Appointment, Invoice, Payment


_REMOVABLE_APPOINTMENT_STATUSES = frozenset(
    {
        Appointment.Status.COMPLETED,
        Appointment.Status.NO_SHOW,
        Appointment.Status.CANCELLED,
    }
)


def admin_may_remove_appointment_from_patient_chart(appointment: Appointment) -> tuple[bool, str]:
    """Whether owner/staff may delete this appointment from the patient history UI."""
    if appointment.status not in _REMOVABLE_APPOINTMENT_STATUSES:
        return (
            False,
            "Only completed, no-show, or cancelled visits can be removed from the patient chart.",
        )
    try:
        invoice = appointment.invoice
    except Invoice.DoesNotExist:
        invoice = None
    if invoice is not None:
        if invoice.status == Invoice.Status.PAID:
            return (
                False,
                "This visit has a paid invoice. Paid visits cannot be removed from the chart.",
            )
        pay_sum = invoice.payments.filter(status=Payment.Status.SUCCESSFUL).aggregate(s=Sum("amount"))["s"]
        if pay_sum and pay_sum > 0:
            return (
                False,
                "This visit has recorded payments. Adjust billing before removing it from the chart.",
            )
    return True, ""


def remove_appointment_from_patient_chart(appointment: Appointment) -> None:
    """Delete appointment + visit + unpaid invoice from the chart (Google Calendar cleanup first)."""
    ok, err = admin_may_remove_appointment_from_patient_chart(appointment)
    if not ok:
        raise ValueError(err)
    from .google_calendar_sync import delete_appointment_google_event_before_db_delete

    with transaction.atomic():
        locked = Appointment.objects.select_for_update().select_related("patient").get(pk=appointment.pk)
        ok, err = admin_may_remove_appointment_from_patient_chart(locked)
        if not ok:
            raise ValueError(err)
        delete_appointment_google_event_before_db_delete(locked)
        locked.delete()
