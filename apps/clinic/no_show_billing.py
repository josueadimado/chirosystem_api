"""Penalty billing: no-show and late cancellation — invoice + optional saved-card charge."""

from __future__ import annotations

import time
from decimal import Decimal

from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import Appointment, ClinicSettings, Invoice, Service, Visit, VisitRenderedService
from .square_payment import try_charge_saved_card

_SYS_NO_SHOW = "SYS-NOSHOW"
_SYS_LATE_CANCEL = "SYS-LATECANCEL"

_PENALTY_KINDS = frozenset({Invoice.Kind.NO_SHOW_FEE, Invoice.Kind.LATE_CANCEL_FEE})


def get_no_show_fee_amount() -> Decimal:
    return ClinicSettings.get_solo().no_show_fee or Decimal("0")


def _get_or_create_penalty_service(*, billing_code: str, name: str, description: str, amount: Decimal) -> Service:
    s, _ = Service.objects.get_or_create(
        billing_code=billing_code,
        defaults={
            "name": name,
            "description": description,
            "duration_minutes": 0,
            "price": amount,
            "is_active": True,
            "show_in_public_booking": False,
            "visible_to_chiropractic_staff": True,
            "visible_to_massage_staff": True,
        },
    )
    return s


def _apply_penalty_fee_for_appointment(
    appointment: Appointment,
    fee: Decimal,
    *,
    invoice_kind: str,
    billing_code: str,
    service_title: str,
    service_description: str,
    visit_doctor_notes: str,
    invoice_prefix: str,
    awaiting_payment_if_charge_fails: bool,
) -> dict:
    """
    Create or reuse a penalty invoice and try charging the patient's saved card.

    Returns:
        already_charged, use_awaiting_payment_instead, clear_checkin
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
        if existing_inv.kind == Invoice.Kind.VISIT:
            raise ValidationError(
                {
                    "detail": (
                        "This appointment already has a clinical bill. Finish or adjust that invoice "
                        "before applying this fee."
                    )
                }
            )
        if existing_inv.kind in _PENALTY_KINDS and existing_inv.kind != invoice_kind:
            raise ValidationError(
                {"detail": "This appointment already has a different penalty invoice; resolve it first."}
            )
        invoice = existing_inv
    else:
        visit_pre = Visit.objects.filter(appointment=appointment).first()
        if visit_pre and visit_pre.rendered_services.exists():
            raise ValidationError(
                {
                    "detail": (
                        "This visit already has service lines. Remove or complete them before "
                        "applying this fee."
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
                "doctor_notes": visit_doctor_notes,
                "completed_at": timezone.now(),
            },
        )
        visit.patient = appointment.patient
        visit.provider = appointment.provider
        visit.status = Visit.Status.COMPLETED
        visit.doctor_notes = visit_doctor_notes
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
        svc = _get_or_create_penalty_service(
            billing_code=billing_code,
            name=service_title,
            description=service_description,
            amount=fee,
        )
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
            invoice_number=f"{invoice_prefix}-{appointment.id}-{int(time.time())}",
            subtotal=fee,
            tax=Decimal("0"),
            discount=Decimal("0"),
            total_amount=fee,
            status=Invoice.Status.ISSUED,
            kind=invoice_kind,
        )

    charge = try_charge_saved_card(invoice)
    if charge["ok"]:
        return {"already_charged": True, "use_awaiting_payment_instead": False, "clear_checkin": True}

    return {
        "already_charged": False,
        "use_awaiting_payment_instead": awaiting_payment_if_charge_fails,
        "clear_checkin": True,
    }


def apply_no_show_fee_for_appointment(appointment: Appointment, fee: Decimal) -> dict:
    """No-show: charge full booked service price (or fallback); card charge or Awaiting payment."""
    return _apply_penalty_fee_for_appointment(
        appointment,
        fee,
        invoice_kind=Invoice.Kind.NO_SHOW_FEE,
        billing_code=_SYS_NO_SHOW,
        service_title="No-show fee",
        service_description="Fee for a missed appointment (system line).",
        visit_doctor_notes="No-show fee (patient missed scheduled appointment).",
        invoice_prefix="INV-NS",
        awaiting_payment_if_charge_fails=True,
    )


def apply_late_cancel_fee_for_appointment(appointment: Appointment, fee: Decimal) -> dict:
    """
    Massage late cancellation (<24h notice): full service price.
    Appointment stays cancelled if charge fails; invoice remains issued (unpaid).
    """
    return _apply_penalty_fee_for_appointment(
        appointment,
        fee,
        invoice_kind=Invoice.Kind.LATE_CANCEL_FEE,
        billing_code=_SYS_LATE_CANCEL,
        service_title="Late cancellation fee",
        service_description="Fee for cancelling a massage with less than 24 hours notice (system line).",
        visit_doctor_notes="Late cancellation fee (massage — under 24 hours notice).",
        invoice_prefix="INV-LC",
        awaiting_payment_if_charge_fails=False,
    )
