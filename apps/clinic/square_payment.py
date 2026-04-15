"""Square Payments, Payment Links, Terminal checkout, and marking invoices paid."""

from __future__ import annotations

import logging
import os
import uuid
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import Appointment, Invoice, Payment
from .square_helpers import ensure_square_customer, get_location_id, get_square_client, get_terminal_device_id

logger = logging.getLogger(__name__)

# Square card payments are typically >= 100 cents in production; keep a small floor for dev.
_MIN_AMOUNT_CENTS = 100


def mark_invoice_paid_from_square(invoice: Invoice, square_payment_id: str) -> bool:
    """
    Record a successful Square payment and close the invoice + appointment.

    Idempotent: safe if the webhook, Terminal poll, and saved-card charge all run
    for the same Square payment id, or if Square retries the same webhook.
    """
    ref = (square_payment_id or "").strip()[:120]
    if not ref:
        logger.warning("mark_invoice_paid_from_square: empty Square payment id")
        return False

    with transaction.atomic():
        inv = (
            Invoice.objects.select_for_update()
            .select_related("patient", "appointment")
            .get(pk=invoice.pk)
        )

        if inv.status == Invoice.Status.PAID:
            return True

        existing = Payment.objects.filter(payment_reference=ref).first()
        if existing:
            if existing.invoice_id != inv.id:
                logger.warning(
                    "Square payment %s already recorded on invoice %s; ignoring invoice %s",
                    ref,
                    existing.invoice_id,
                    inv.id,
                )
                return False
        else:
            Payment.objects.create(
                invoice=inv,
                patient=inv.patient,
                amount=inv.total_amount,
                payment_method=Payment.Method.CARD,
                payment_reference=ref,
                status=Payment.Status.SUCCESSFUL,
                paid_at=timezone.now(),
            )

        if inv.status != Invoice.Status.PAID:
            inv.status = Invoice.Status.PAID
            inv.paid_at = timezone.now()
            inv.save(update_fields=["status", "paid_at", "updated_at"])

        appt = inv.appointment
        target_status = (
            Appointment.Status.NO_SHOW
            if inv.kind == Invoice.Kind.NO_SHOW_FEE
            else Appointment.Status.COMPLETED
        )
        if appt.status != target_status:
            appt.status = target_status
            if not appt.completed_at:
                appt.completed_at = timezone.now()
            appt.save(update_fields=["status", "completed_at", "updated_at"])

    return True


def _money_cents(invoice: Invoice) -> int:
    return int(Decimal(invoice.total_amount) * 100)


def try_charge_saved_card(invoice: Invoice) -> dict:
    """
    Charge the patient's Square card on file (card-present not required).
    Returns {"ok": bool, "error": str | None, "payment_intent_id": str | None}
    (payment_intent_id holds Square payment id for API compatibility with the web UI.)
    """
    from square.requests.money import MoneyParams

    patient = invoice.patient
    if not patient.square_customer_id or not patient.square_card_id:
        return {"ok": False, "error": "no_saved_card", "payment_intent_id": None}

    loc = get_location_id()
    if not loc:
        return {"ok": False, "error": "square_location_not_configured", "payment_intent_id": None}

    amount_cents = _money_cents(invoice)
    if amount_cents < _MIN_AMOUNT_CENTS:
        return {"ok": False, "error": "amount_below_minimum", "payment_intent_id": None}

    client = get_square_client()
    try:
        res = client.payments.create(
            source_id=patient.square_card_id,
            idempotency_key=str(uuid.uuid4()),
            amount_money=MoneyParams(amount=amount_cents, currency="USD"),
            customer_id=patient.square_customer_id,
            location_id=loc,
            reference_id=str(invoice.id)[:40],
            autocomplete=True,
            note=f"Invoice {invoice.invoice_number}",
        )
        if res.errors:
            err = res.errors[0].detail if res.errors else "payment failed"
            return {"ok": False, "error": err[:500], "payment_intent_id": None}
        pay = res.payment
        if pay and pay.status == "COMPLETED" and pay.id:
            mark_invoice_paid_from_square(invoice, pay.id)
            return {"ok": True, "error": None, "payment_intent_id": pay.id}
        return {
            "ok": False,
            "error": f"payment_status_{getattr(pay, 'status', 'unknown')}",
            "payment_intent_id": getattr(pay, "id", None),
        }
    except Exception as exc:
        logger.warning("Square saved card charge failed: %s", exc)
        return {"ok": False, "error": str(exc)[:500], "payment_intent_id": None}


def create_payment_link_for_invoice(
    invoice: Invoice,
    success_url: str,
    *,
    cancel_url: str | None = None,
) -> str | None:
    """
    Hosted Square checkout (payment link) for the patient.

    Square's documented CheckoutOptions only include ``redirect_url`` (after a successful
    payment). If ``cancel_url`` is set, it is sent as an extra field for forward
    compatibility; older Square API versions ignore unknown keys. If creation fails,
    clear ``cancel_url`` in the caller or unset ``SQUARE_CHECKOUT_SEND_CANCEL_URL``.
    """
    from square.requests.money import MoneyParams
    from square.requests.order import OrderParams
    from square.requests.order_line_item import OrderLineItemParams

    loc = get_location_id()
    if not loc:
        return None

    patient = invoice.patient
    ensure_square_customer(patient)
    amount_cents = _money_cents(invoice)
    if amount_cents < _MIN_AMOUNT_CENTS:
        return None

    client = get_square_client()
    order = OrderParams(
        location_id=loc,
        reference_id=str(invoice.id)[:40],
        line_items=[
            OrderLineItemParams(
                quantity="1",
                name=f"Invoice {invoice.invoice_number}",
                item_type="ITEM",
                base_price_money=MoneyParams(amount=amount_cents, currency="USD"),
            )
        ],
    )
    from django.conf import settings as dj_settings

    send_cancel = bool(cancel_url and getattr(dj_settings, "SQUARE_CHECKOUT_SEND_CANCEL_URL", False))
    checkout_options: dict = {"redirect_url": success_url}
    if send_cancel and cancel_url:
        checkout_options["cancel_url"] = cancel_url

    res = client.checkout.payment_links.create(
        idempotency_key=str(uuid.uuid4()),
        description=f"Invoice {invoice.invoice_number}",
        order=order,
        checkout_options=checkout_options,
    )
    if res.errors:
        logger.warning("Square payment link error: %s", res.errors)
        return None
    pl = res.payment_link
    if pl and pl.url:
        return pl.url
    return None


def create_terminal_checkout_for_invoice(invoice: Invoice) -> dict:
    """
    Send a card-present payment to the configured Square Terminal device.
    Returns {"checkout_id": str, "status": str} or raises on error.
    """
    from square.requests.device_checkout_options import DeviceCheckoutOptionsParams
    from square.requests.money import MoneyParams
    from square.requests.terminal_checkout import TerminalCheckoutParams

    device_id = get_terminal_device_id()
    if not device_id:
        raise ValueError("SQUARE_DEVICE_ID is not set — pair your Terminal in the Square Dashboard and paste the device id.")

    amount_cents = _money_cents(invoice)
    if amount_cents < _MIN_AMOUNT_CENTS:
        raise ValueError("Amount is below the minimum for card processing.")

    client = get_square_client()
    res = client.terminal.checkouts.create(
        idempotency_key=str(uuid.uuid4()),
        checkout=TerminalCheckoutParams(
            amount_money=MoneyParams(amount=amount_cents, currency="USD"),
            reference_id=str(invoice.id)[:40],
            note=f"Invoice {invoice.invoice_number}",
            device_options=DeviceCheckoutOptionsParams(device_id=device_id),
            payment_type="CARD_PRESENT",
        ),
    )
    if res.errors:
        raise RuntimeError(res.errors[0].detail if res.errors else "Terminal checkout failed")
    co = res.checkout
    if not co or not co.id:
        raise RuntimeError("Square did not return a terminal checkout id.")
    return {"checkout_id": co.id, "status": getattr(co, "status", None) or "PENDING"}


def get_terminal_checkout_status(checkout_id: str) -> dict:
    """Poll Terminal checkout; if completed, mark invoice paid when possible."""
    client = get_square_client()
    res = client.terminal.checkouts.get(checkout_id)
    if res.errors:
        return {"checkout_id": checkout_id, "status": "ERROR", "error": res.errors[0].detail if res.errors else "unknown"}
    co = res.checkout
    if not co:
        return {"checkout_id": checkout_id, "status": "UNKNOWN"}
    st = getattr(co, "status", None) or "UNKNOWN"
    out: dict = {"checkout_id": checkout_id, "status": st}
    if st == "COMPLETED" and co.payment_ids:
        pid = co.payment_ids[0]
        out["payment_id"] = pid
        ref = (co.reference_id or "").strip()
        if ref.isdigit():
            inv = Invoice.objects.filter(pk=int(ref), status=Invoice.Status.ISSUED).first()
            if inv:
                mark_invoice_paid_from_square(inv, pid)
    return out


def get_frontend_base_url() -> str:
    try:
        from django.conf import settings

        base = getattr(settings, "FRONTEND_BASE_URL", None) or os.environ.get(
            "FRONTEND_BASE_URL", "http://localhost:3001"
        )
    except Exception:
        base = os.environ.get("FRONTEND_BASE_URL", "http://localhost:3001")
    return str(base).rstrip("/")


def build_invoice_payment_followup_dict(invoice: Invoice, *, try_saved_card: bool) -> dict:
    from .square_helpers import square_configured

    invoice.refresh_from_db()
    if invoice.status == Invoice.Status.PAID:
        return {
            "invoice_id": invoice.id,
            "invoice_number": invoice.invoice_number,
            "total_amount": str(invoice.total_amount),
            "already_paid": True,
            "payment": {
                "status": "charged_saved_card",
                "charged": True,
                "checkout_url": None,
                "charge_error": None,
                "payment_intent_id": None,
            },
        }

    payment: dict = {
        "status": "manual",
        "charged": False,
        "checkout_url": None,
        "charge_error": None,
        "payment_intent_id": None,
    }

    if not square_configured():
        payment["status"] = "square_not_configured"
        return {
            "invoice_id": invoice.id,
            "invoice_number": invoice.invoice_number,
            "total_amount": str(invoice.total_amount),
            "already_paid": False,
            "payment": payment,
        }

    base = get_frontend_base_url()
    success_url = f"{base}/payment/success?square=1&invoice={invoice.id}"
    cancel_url = f"{base}/payment/cancel?square=1&invoice={invoice.id}"

    if try_saved_card:
        charge_result = try_charge_saved_card(invoice)
        if charge_result["ok"]:
            invoice.refresh_from_db()
            return {
                "invoice_id": invoice.id,
                "invoice_number": invoice.invoice_number,
                "total_amount": str(invoice.total_amount),
                "already_paid": True,
                "payment": {
                    "status": "charged_saved_card",
                    "charged": True,
                    "checkout_url": None,
                    "charge_error": None,
                    "payment_intent_id": charge_result.get("payment_intent_id"),
                },
            }
        payment["charge_error"] = charge_result.get("error")

    checkout_url = create_payment_link_for_invoice(invoice, success_url, cancel_url=cancel_url)
    if checkout_url:
        payment["status"] = "checkout_link"
        payment["checkout_url"] = checkout_url
    else:
        payment["status"] = "awaiting_manual"

    return {
        "invoice_id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "total_amount": str(invoice.total_amount),
        "already_paid": False,
        "payment": payment,
    }
