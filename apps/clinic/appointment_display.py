"""Shared appointment status for API responses and UI (schedule, dashboards)."""

from __future__ import annotations

from apps.clinic.models import Appointment, Invoice


def appointment_ui_status(appt: Appointment, *, invoice_kind: str | None = None) -> str:
    """
    Status label for calendars and lists.
    Legacy rows may be awaiting_payment with a no-show fee invoice — show as no_show.
    Pass invoice_kind when the invoice was loaded separately (e.g. doctor dashboard batch).
    """
    if appt.status == Appointment.Status.AWAITING_PAYMENT:
        kind = invoice_kind
        if kind is None:
            try:
                kind = appt.invoice.kind
            except Invoice.DoesNotExist:
                kind = None
        if kind == Invoice.Kind.NO_SHOW_FEE:
            return Appointment.Status.NO_SHOW
    return appt.status
