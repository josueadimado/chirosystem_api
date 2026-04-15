"""No-show fee: create invoice, charge saved card if present, else leave appointment awaiting payment."""

from __future__ import annotations

import time
from decimal import Decimal

from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import Appointment, ClinicSettings, Invoice, Service, Visit, VisitRenderedService
from .square_payment import try_charge_saved_card

_SYS_BILLING_CODE = "SYS-NOSHOW"


def get_no_show_fee_amount() -> Decimal:
    return ClinicSettings.get_solo().no_show_fee or Decimal("0")


def _get_or_create_no_show_service(amount: Decimal) -> Service:
    s, _ = Service.objects.get_or_create(
        billing_code=_SYS_BILLING_CODE,
        defaults={
            "name": "No-show fee",
            "description": "Fee for a missed appointment (system line).",
            "duration_minutes": 0,
            "price": amount,
            "is_active": True,
            "show_in_public_booking": False,
            "visible_to_chiropractic_staff": True,
            "visible_to_massage_staff": True,
        },
    )
    return s


def apply_no_show_fee_for_appointment(appointment: Appointment, fee: Decimal) -> dict:
    """
    Expects caller to hold a DB lock on this appointment row when needed.

    Creates (or reuses) a no-show fee invoice, then attempts ``try_charge_saved_card``.

    Returns:
        already_charged: Saved-card charge succeeded (appointment + invoice updated).
        use_awaiting_payment_instead: No card / charge failed — PATCH should use ``awaiting_payment``.
        clear_checkin: Clear check-in timestamps on the appointment.
    """
    if fee <= 0:
        return {
            "already_charged": False,
            "use_awaiting_payment_instead": False,
            "clear_checkin": False,
        }

    existing_inv = Invoice.objects.filter(appointment=appointment).first()
    if existing_inv:
        if existing_inv.status == Invoice.Status.PAID:
            return {
                "already_charged": False,
                "use_awaiting_payment_instead": False,
                "clear_checkin": True,
            }
        if existing_inv.kind != Invoice.Kind.NO_SHOW_FEE:
            raise ValidationError(
                {
                    "detail": (
                        "This appointment already has a clinical bill. Finish or adjust that invoice "
                        "before applying a no-show fee."
                    )
                }
            )
        invoice = existing_inv
    else:
        visit_pre = Visit.objects.filter(appointment=appointment).first()
        if visit_pre and visit_pre.rendered_services.exists():
            raise ValidationError(
                {
                    "detail": (
                        "This visit already has service lines. Remove or complete them before "
                        "marking no-show with a fee."
                    )
                }
            )
        visit, _ = Visit.objects.get_or_create(
            appointment=appointment,
            defaults={
                "patient": appointment.patient,
                "provider": appointment.provider,
                "status": Visit.Status.COMPLETED,
                "reason_for_visit": "",
                "doctor_notes": "No-show fee (patient missed scheduled appointment).",
                "completed_at": timezone.now(),
            },
        )
        visit.patient = appointment.patient
        visit.provider = appointment.provider
        visit.status = Visit.Status.COMPLETED
        visit.doctor_notes = "No-show fee (patient missed scheduled appointment)."
        visit.completed_at = timezone.now()
        visit.save(
            update_fields=[
                "patient",
                "provider",
                "status",
                "doctor_notes",
                "completed_at",
                "updated_at",
            ]
        )
        VisitRenderedService.objects.filter(visit=visit).delete()
        svc = _get_or_create_no_show_service(fee)
        VisitRenderedService.objects.create(
            visit=visit,
            service=svc,
            quantity=1,
            unit_price=fee,
            total_price=fee,
        )
        invoice = Invoice.objects.create(
            patient=appointment.patient,
            appointment=appointment,
            visit=visit,
            invoice_number=f"INV-NS-{appointment.id}-{int(time.time())}",
            subtotal=fee,
            tax=Decimal("0"),
            discount=Decimal("0"),
            total_amount=fee,
            status=Invoice.Status.ISSUED,
            kind=Invoice.Kind.NO_SHOW_FEE,
        )

    charge = try_charge_saved_card(invoice)
    if charge["ok"]:
        return {"already_charged": True, "use_awaiting_payment_instead": False, "clear_checkin": True}

    return {"already_charged": False, "use_awaiting_payment_instead": True, "clear_checkin": True}
