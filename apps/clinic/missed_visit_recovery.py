"""Reopen missed visits (forgot check-in / mistaken no-show) so staff can check in and doctors can complete."""

from decimal import Decimal

from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import Appointment, Invoice, Visit, VisitRenderedService


def _penalty_invoice_for_appointment(appointment: Appointment) -> Invoice | None:
    try:
        inv = appointment.invoice
    except Invoice.DoesNotExist:
        return None
    if inv.kind in (Invoice.Kind.NO_SHOW_FEE, Invoice.Kind.LATE_CANCEL_FEE):
        return inv
    return None


def _visit_invoice_blocks_reopen(appointment: Appointment) -> Invoice | None:
    try:
        inv = appointment.invoice
    except Invoice.DoesNotExist:
        return None
    if inv.kind == Invoice.Kind.VISIT and inv.status != Invoice.Status.VOID:
        return inv
    return None


def _invoice_has_recorded_payments(invoice: Invoice) -> bool:
    from .invoice_collection import invoice_payment_summary

    paid = Decimal(str(invoice_payment_summary(invoice).get("amount_paid", "0") or "0"))
    return paid > Decimal("0.01")


def appointment_is_missed_no_show(appointment: Appointment) -> bool:
    if appointment.status == Appointment.Status.NO_SHOW:
        return True
    if appointment.status == Appointment.Status.AWAITING_PAYMENT:
        inv = _penalty_invoice_for_appointment(appointment)
        return inv is not None and inv.kind == Invoice.Kind.NO_SHOW_FEE
    return False


def staff_may_reopen_missed_visit(appointment: Appointment) -> tuple[bool, str]:
    """Whether desk staff may reopen a mistaken no-show for a real visit."""
    if not appointment_is_missed_no_show(appointment):
        return False, "Only no-show visits (or awaiting payment on a no-show fee) can be reopened."

    visit_inv = _visit_invoice_blocks_reopen(appointment)
    if visit_inv is not None:
        if visit_inv.status == Invoice.Status.PAID or _invoice_has_recorded_payments(visit_inv):
            return False, "This visit already has a paid clinical bill. Adjust billing instead of reopening."
        return False, "This visit already has a clinical bill on file. Use billing tools instead of reopening."

    penalty = _penalty_invoice_for_appointment(appointment)
    if penalty is not None:
        if penalty.status == Invoice.Status.PAID:
            return (
                False,
                "The no-show fee was already paid. Book a new visit or handle billing separately.",
            )
        if _invoice_has_recorded_payments(penalty):
            return (
                False,
                "Payments were recorded on the no-show fee. Resolve billing before reopening this visit.",
            )
    return True, ""


def _void_penalty_invoice(invoice: Invoice) -> None:
    if invoice.status != Invoice.Status.VOID:
        invoice.status = Invoice.Status.VOID
        invoice.save(update_fields=["status", "updated_at"])


def _reset_visit_after_penalty(appointment: Appointment) -> None:
    visit = Visit.objects.filter(appointment=appointment).first()
    if not visit:
        return
    VisitRenderedService.objects.filter(visit=visit).delete()
    visit.status = Visit.Status.OPEN
    visit.doctor_notes = ""
    visit.completed_at = None
    visit.save(update_fields=["status", "doctor_notes", "completed_at", "updated_at"])


def _apply_missed_visit_reopen_side_effects(appointment: Appointment) -> None:
    penalty = _penalty_invoice_for_appointment(appointment)
    if penalty is not None:
        _void_penalty_invoice(penalty)
    _reset_visit_after_penalty(appointment)
    appointment.checked_in_at = None
    appointment.consultation_started_at = None
    appointment.completed_at = None
    appointment.auto_no_show_processed_at = None


def prepare_admin_reopen_missed_visit(appointment: Appointment) -> None:
    """Validate and clear penalty billing before PATCH sets status back to booked."""
    ok, err = staff_may_reopen_missed_visit(appointment)
    if not ok:
        raise ValidationError({"detail": err})
    _apply_missed_visit_reopen_side_effects(appointment)


def reopen_missed_visit_to_booked(appointment: Appointment) -> None:
    """Reopen a mistaken no-show and return the appointment to booked."""
    prepare_admin_reopen_missed_visit(appointment)
    appointment.status = Appointment.Status.BOOKED
    appointment.save(
        update_fields=[
            "status",
            "checked_in_at",
            "consultation_started_at",
            "completed_at",
            "auto_no_show_processed_at",
            "updated_at",
        ]
    )


def staff_may_checkin_appointment_date(appointment_date, *, today) -> tuple[bool, str]:
    """Staff desk check-in: today or past dates OK; future dates blocked."""
    if appointment_date > today:
        return (
            False,
            "Check-in for a future appointment is not available yet. "
            "Move the visit to today on the schedule first.",
        )
    return True, ""


def staff_desk_checkin_appointment(appointment: Appointment, *, now=None) -> Appointment:
    """Reopen mistaken no-shows if needed, then mark checked in."""
    now = now or timezone.now()
    locked = Appointment.objects.select_for_update().get(pk=appointment.pk)
    if appointment_is_missed_no_show(locked):
        reopen_missed_visit_to_booked(locked)
        locked.refresh_from_db()
    if locked.status != Appointment.Status.BOOKED:
        raise ValidationError({"detail": "Check-in is already done or this visit cannot be checked in."})
    locked.status = Appointment.Status.CHECKED_IN
    locked.checked_in_at = now
    locked.save(update_fields=["status", "checked_in_at", "updated_at"])
    return locked
