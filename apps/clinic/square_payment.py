"""Square Payments, Payment Links, Terminal checkout, and marking invoices paid."""

from __future__ import annotations

import logging
import os
import uuid
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import Appointment, Invoice, Patient, PatientCreditTransaction, Payment
from .square_helpers import (
    ensure_square_customer,
    get_kiosk_terminal_device_id,
    get_location_id,
    get_square_client,
    get_terminal_device_id,
    square_configured,
    square_search_orders,
)

logger = logging.getLogger(__name__)

# Square card payments are typically >= 100 cents in production; keep a small floor for dev.
_MIN_AMOUNT_CENTS = 100
_CREDIT_TOPUP_REF_PREFIX = "ct_"
_BUNDLE_REF_PREFIX = "bundle-"
_OPEN_INVOICE_STATUSES = (Invoice.Status.ISSUED, Invoice.Status.OVERDUE, Invoice.Status.DRAFT)


def invoice_id_from_square_reference(ref: str | None) -> int | None:
    """Parse Square reference_id: plain invoice pk or bundle-{pk} for multi-invoice checkout."""
    if not ref:
        return None
    ref = ref.strip()
    if ref.startswith(_BUNDLE_REF_PREFIX):
        tail = ref[len(_BUNDLE_REF_PREFIX) :].strip()
        if tail.isdigit():
            try:
                return int(tail)
            except ValueError:
                return None
    if ref.isdigit():
        try:
            return int(ref)
        except ValueError:
            return None
    return None


def bundle_reference_id(primary_invoice_id: int) -> str:
    return f"{_BUNDLE_REF_PREFIX}{primary_invoice_id}"[:40]


def invoice_open_for_square_settlement(invoice_id: int) -> Invoice | None:
    """Invoice still expecting payment — used by webhooks, polling, and Square reconcile."""
    return (
        Invoice.objects.filter(pk=invoice_id, status__in=_OPEN_INVOICE_STATUSES)
        .select_related("appointment", "patient")
        .first()
    )


def mark_invoices_paid_from_square_bundle(invoice_ids: list[int], square_payment_id: str) -> bool:
    """Mark each open invoice paid from one Square payment (unique payment_reference per invoice)."""
    ref_base = (square_payment_id or "").strip()[:120]
    if not ref_base or not invoice_ids:
        return False
    ok = False
    for inv_id in invoice_ids:
        inv = Invoice.objects.filter(pk=inv_id).first()
        if not inv or inv.status == Invoice.Status.PAID:
            continue
        if mark_invoice_paid_from_square(inv, ref_base, payment_reference=f"{ref_base}#inv{inv_id}"):
            ok = True
    return ok


def mark_invoice_paid_from_square(
    invoice: Invoice,
    square_payment_id: str,
    *,
    payment_reference: str | None = None,
) -> bool:
    """
    Record a successful Square payment and close the invoice + appointment.

    Idempotent: safe if the webhook, Terminal poll, and saved-card charge all run
    for the same Square payment id, or if Square retries the same webhook.
    """
    ref = (payment_reference or square_payment_id or "").strip()[:120]
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
        if inv.kind == Invoice.Kind.NO_SHOW_FEE:
            target_status = Appointment.Status.NO_SHOW
        elif inv.kind == Invoice.Kind.LATE_CANCEL_FEE:
            target_status = Appointment.Status.CANCELLED
        else:
            target_status = Appointment.Status.COMPLETED
        if appt.status != target_status:
            appt.status = target_status
            if target_status == Appointment.Status.CANCELLED:
                appt.completed_at = None
            elif not appt.completed_at:
                appt.completed_at = timezone.now()
            appt.save(update_fields=["status", "completed_at", "updated_at"])

    return True


def _payment_reference_matches_invoice(payment_ref: str | None, invoice_pk: int) -> bool:
    ref = (payment_ref or "").strip()
    if not ref:
        return False
    if ref == str(invoice_pk):
        return True
    if ref.isdigit() and int(ref) == invoice_pk:
        return True
    if ref.startswith(_BUNDLE_REF_PREFIX):
        primary = invoice_id_from_square_reference(ref)
        return primary == invoice_pk
    if "#inv" in ref:
        suffix = ref.split("#inv", 1)[-1].strip()
        if suffix.isdigit() and int(suffix) == invoice_pk:
            return True
    return False


def _payment_note_matches_invoice(note: str | None, invoice: Invoice) -> bool:
    """Terminal / POS payments often carry the invoice number in the note, not reference_id."""
    text = (note or "").strip()
    if not text or not invoice.invoice_number:
        return False
    inv_no = invoice.invoice_number.strip()
    if inv_no in text:
        return True
    if f"Invoice {inv_no}" in text:
        return True
    return False


def _order_reference_matches_invoice(client, order_id: str | None, invoice_pk: int) -> bool:
    if not order_id:
        return False
    try:
        res = client.orders.get(order_id=order_id)
        order = getattr(res, "order", None)
        if not order:
            return False
        return _payment_reference_matches_invoice(getattr(order, "reference_id", None), invoice_pk)
    except Exception as exc:
        logger.debug("Square order %s lookup for invoice %s: %s", order_id, invoice_pk, exc)
        return False


def _order_dict_matches_invoice(order: dict, invoice: Invoice) -> bool:
    if _payment_reference_matches_invoice(order.get("reference_id"), invoice.pk):
        return True
    for li in order.get("line_items") or []:
        if isinstance(li, dict) and _payment_note_matches_invoice(li.get("name"), invoice):
            return True
    return False


def _payment_ids_from_order_dict(order: dict) -> list[str]:
    ids: list[str] = []
    for t in order.get("tenders") or []:
        if not isinstance(t, dict):
            continue
        pid = t.get("payment_id")
        if pid:
            ids.append(str(pid).strip())
    return ids


def _payment_ids_from_order_obj(order) -> list[str]:
    ids: list[str] = []
    for t in getattr(order, "tenders", None) or []:
        pid = getattr(t, "payment_id", None)
        if pid:
            ids.append(str(pid).strip())
    return ids


def _order_obj_matches_invoice(order, invoice: Invoice) -> bool:
    if _payment_reference_matches_invoice(getattr(order, "reference_id", None), invoice.pk):
        return True
    for li in getattr(order, "line_items", None) or []:
        name = getattr(li, "name", None) if not isinstance(li, dict) else li.get("name")
        if _payment_note_matches_invoice(name, invoice):
            return True
    return False


def _square_payment_matches_invoice(client, pay, invoice: Invoice) -> bool:
    if (getattr(pay, "status", None) or "").upper() != "COMPLETED":
        return False
    if _payment_reference_matches_invoice(getattr(pay, "reference_id", None), invoice.pk):
        return True
    if _payment_note_matches_invoice(getattr(pay, "note", None), invoice):
        return True
    order_id = getattr(pay, "order_id", None)
    if order_id and _order_reference_matches_invoice(client, order_id, invoice.pk):
        return True
    return False


def _payment_relaxed_cross_device_match(pay, invoice: Invoice, patient: Patient) -> bool:
    """
    Match a payment taken on another Square device (iPad, phone app, second reader) that may
    not include our invoice reference — same patient, amount, and visit day only.
    """
    if (getattr(pay, "status", None) or "").upper() != "COMPLETED":
        return False
    amount_cents = _money_cents(invoice)
    am = getattr(pay, "amount_money", None)
    paid_cents = int(getattr(am, "amount", 0) or 0) if am else 0
    if paid_cents < amount_cents:
        return False
    appt = invoice.appointment
    if appt and appt.appointment_date:
        created = str(getattr(pay, "created_at", None) or "")
        if str(appt.appointment_date) not in created:
            return False
    cid = (patient.square_customer_id or "").strip()
    pay_cid = (getattr(pay, "customer_id", None) or "").strip()
    if cid and pay_cid == cid:
        return True
    note = (getattr(pay, "note", None) or "").lower()
    fn = (patient.first_name or "").strip().lower()
    ln = (patient.last_name or "").strip().lower()
    if len(fn) >= 2 and len(ln) >= 2 and fn in note and ln in note:
        return True
    return False


def _order_relaxed_cross_device_match(order: dict, invoice: Invoice, patient: Patient) -> bool:
    if _order_dict_matches_invoice(order, invoice):
        return True
    total = order.get("total_money") or {}
    order_cents = total.get("amount") if isinstance(total, dict) else None
    if not isinstance(order_cents, int) or order_cents < _money_cents(invoice):
        return False
    created = str(order.get("created_at") or "")
    appt = invoice.appointment
    if appt and appt.appointment_date and str(appt.appointment_date) not in created:
        return False
    fn = (patient.first_name or "").strip().lower()
    ln = (patient.last_name or "").strip().lower()
    if len(fn) < 2 or len(ln) < 2:
        return False
    for li in order.get("line_items") or []:
        if not isinstance(li, dict):
            continue
        name = (li.get("name") or "").lower()
        if fn in name and ln in name:
            return True
    return False


def _iter_terminal_checkouts(
    client,
    *,
    reference_ids: list[str] | None = None,
    statuses: tuple[str, ...] = ("COMPLETED",),
    max_pages: int = 10,
):
    """All Terminal checkouts at the business — any paired device, not only our kiosk."""
    from square.requests.search_terminal_checkouts_request import SearchTerminalCheckoutsRequest
    from square.requests.terminal_checkout_filter import TerminalCheckoutFilterParams
    from square.requests.terminal_checkout_query import TerminalCheckoutQueryParams

    cursor = None
    for _ in range(max_pages):
        filt_kwargs: dict = {"statuses": list(statuses)}
        if reference_ids:
            filt_kwargs["reference_ids"] = reference_ids
        body = SearchTerminalCheckoutsRequest(
            query=TerminalCheckoutQueryParams(filter=TerminalCheckoutFilterParams(**filt_kwargs)),
            cursor=cursor,
        )
        res = client.terminal.checkouts.search(body=body)
        checkouts = getattr(res, "checkouts", None) or []
        for co in checkouts:
            yield co
        cursor = getattr(res, "cursor", None)
        if not cursor:
            break


def _iter_square_payments(client, *, begin_str: str, loc: str | None, amount_cents: int | None, max_pages: int = 8):
    cursor = None
    for _ in range(max_pages):
        list_kwargs: dict = {
            "begin_time": begin_str,
            "sort_order": "DESC",
            "limit": 100,
        }
        if loc:
            list_kwargs["location_id"] = loc
        if amount_cents is not None and amount_cents >= _MIN_AMOUNT_CENTS:
            list_kwargs["total"] = amount_cents
        if cursor:
            list_kwargs["cursor"] = cursor
        res = client.payments.list(**list_kwargs)
        payments = getattr(res, "payments", None) or []
        for pay in payments:
            yield pay
        cursor = getattr(res, "cursor", None)
        if not cursor:
            break


def staff_confirm_invoice_paid(invoice: Invoice) -> bool:
    """
    Record payment when staff verified paid in the Square app but automatic sync could not
    match the payment (missing reference / note in Square API).
    """
    ref = f"staff_confirm:{invoice.id}:{int(timezone.now().timestamp())}"[:120]
    return mark_invoice_paid_from_square(invoice, ref)


def sync_invoice_payment_from_square(invoice: Invoice | int) -> dict:
    """Force Square reconcile; returns status for UI."""
    if isinstance(invoice, int):
        inv = Invoice.objects.filter(pk=invoice).select_related("appointment", "patient").first()
    else:
        inv = Invoice.objects.filter(pk=invoice.pk).select_related("appointment", "patient").first()
    if not inv:
        return {"ok": False, "paid": False, "detail": "Invoice not found."}
    if inv.status == Invoice.Status.PAID:
        return {
            "ok": True,
            "paid": True,
            "detail": "Invoice is already marked paid.",
            "invoice_status": inv.status,
            "appointment_status": inv.appointment.status if inv.appointment_id else None,
        }
    if not square_configured():
        return {
            "ok": False,
            "paid": False,
            "detail": "Square is not configured on the server (missing SQUARE_ACCESS_TOKEN).",
            "invoice_status": inv.status,
        }
    before = inv.status
    try_reconcile_invoice_from_square(inv)
    inv.refresh_from_db()
    if inv.appointment_id:
        inv.appointment.refresh_from_db()
    paid = inv.status == Invoice.Status.PAID
    if paid:
        detail = "Payment found in Square and recorded."
    elif before == inv.status:
        detail = (
            "Square did not return a matching payment for this invoice yet "
            "(we checked all devices at your clinic location: readers, iPad, Square app, etc.). "
            "If the Square app shows paid, use “Mark paid (verified in Square)” or check sandbox vs production mode."
        )
    else:
        detail = f"Invoice status is now {inv.status}."
    return {
        "ok": paid,
        "paid": paid,
        "detail": detail,
        "invoice_status": inv.status,
        "appointment_status": inv.appointment.status if inv.appointment_id else None,
        "square_configured": True,
    }


def reconcile_open_invoices_for_patient(patient: Patient | int) -> int:
    """Try to sync unpaid visit invoices from Square when opening patient chart/history."""
    if isinstance(patient, int):
        patient = Patient.objects.filter(pk=patient).first()
    if not patient:
        return 0
    open_invs = (
        Invoice.objects.filter(
            patient=patient,
            kind=Invoice.Kind.VISIT,
            status__in=_OPEN_INVOICE_STATUSES,
        )
        .order_by("-created_at")[:20]
    )
    fixed = 0
    for inv in open_invs:
        if try_reconcile_invoice_from_square(inv):
            fixed += 1
    return fixed


def try_reconcile_invoice_from_square(invoice: Invoice | int) -> bool:
    """
    If Square already captured payment for this invoice (Terminal / webhook delay),
    mark the invoice and appointment paid locally. Safe to call repeatedly.
    """
    if isinstance(invoice, int):
        inv = (
            Invoice.objects.filter(pk=invoice)
            .select_related("appointment", "patient", "visit")
            .first()
        )
    else:
        inv = (
            Invoice.objects.filter(pk=invoice.pk)
            .select_related("appointment", "patient", "visit")
            .first()
        )
    if not inv:
        return False
    if inv.status == Invoice.Status.PAID:
        return True
    if try_reconcile_bundle_terminal_payment(inv):
        inv.refresh_from_db()
        if inv.status == Invoice.Status.PAID:
            return True
    if not square_configured():
        return False

    ref = str(inv.pk)
    loc = get_location_id()
    amount_cents = _money_cents(inv)
    client = get_square_client()
    appt_date = inv.appointment.appointment_date if inv.appointment_id else None

    if loc:
        try:
            for order in square_search_orders(location_ids=[loc], reference_ids=[ref]):
                if not _order_dict_matches_invoice(order, inv):
                    continue
                for pid in _payment_ids_from_order_dict(order):
                    if mark_invoice_paid_from_square(inv, pid):
                        logger.info("Reconciled invoice %s from Square order %s", inv.pk, order.get("id"))
                        return True
            patient = inv.patient
            cid = (patient.square_customer_id or "").strip() if patient else ""
            if cid:
                order_candidates: list[dict] = []
                for order in square_search_orders(location_ids=[loc], customer_ids=[cid]):
                    if _order_dict_matches_invoice(order, inv):
                        for pid in _payment_ids_from_order_dict(order):
                            if mark_invoice_paid_from_square(inv, pid):
                                logger.info(
                                    "Reconciled invoice %s from customer order %s",
                                    inv.pk,
                                    order.get("id"),
                                )
                                return True
                    if patient and _order_relaxed_cross_device_match(order, inv, patient):
                        order_candidates.append(order)
                if patient and len(order_candidates) == 1:
                    for pid in _payment_ids_from_order_dict(order_candidates[0]):
                        if mark_invoice_paid_from_square(inv, pid):
                            logger.info(
                                "Reconciled invoice %s from cross-device order %s",
                                inv.pk,
                                order_candidates[0].get("id"),
                            )
                            return True
        except Exception as exc:
            logger.debug("Square orders search for invoice %s: %s", inv.pk, exc)

    try:
        for co in _iter_terminal_checkouts(client, reference_ids=[ref]):
            payment_ids = getattr(co, "payment_ids", None) or []
            if payment_ids and mark_invoice_paid_from_square(inv, payment_ids[0]):
                logger.info("Reconciled invoice %s from completed Terminal checkout %s", inv.pk, co.id)
                return True
        for co in _iter_terminal_checkouts(client, reference_ids=None):
            note = getattr(co, "note", None) or ""
            checkout_ref = getattr(co, "reference_id", None)
            if not _payment_note_matches_invoice(note, inv) and not _payment_reference_matches_invoice(
                checkout_ref, inv.pk
            ):
                continue
            payment_ids = getattr(co, "payment_ids", None) or []
            if payment_ids and mark_invoice_paid_from_square(inv, payment_ids[0]):
                logger.info(
                    "Reconciled invoice %s from Terminal checkout %s on another device",
                    inv.pk,
                    co.id,
                )
                return True
    except Exception as exc:
        logger.debug("Terminal checkout search for invoice %s: %s", inv.pk, exc)

    patient = inv.patient
    cid = (patient.square_customer_id or "").strip() if patient else ""
    if cid and amount_cents >= _MIN_AMOUNT_CENTS:
        try:
            begin = (inv.created_at or timezone.now()) - timedelta(days=7)
            begin_str = begin.strftime("%Y-%m-%dT%H:%M:%SZ")
            cursor = None
            candidates: list = []
            for _ in range(6):
                list_kwargs: dict = {
                    "begin_time": begin_str,
                    "sort_order": "DESC",
                    "limit": 100,
                    "customer_id": cid,
                }
                if loc:
                    list_kwargs["location_id"] = loc
                if cursor:
                    list_kwargs["cursor"] = cursor
                res = client.payments.list(**list_kwargs)
                payments = getattr(res, "payments", None) or []
                for pay in payments:
                    if (getattr(pay, "status", None) or "").upper() != "COMPLETED":
                        continue
                    am = getattr(pay, "amount_money", None)
                    paid_cents = int(getattr(am, "amount", 0) or 0) if am else 0
                    if paid_cents < amount_cents:
                        continue
                    if _square_payment_matches_invoice(client, pay, inv):
                        pid = getattr(pay, "id", None)
                        if pid and mark_invoice_paid_from_square(inv, pid):
                            logger.info("Reconciled invoice %s from customer payment %s", inv.pk, pid)
                            return True
                    if appt_date and paid_cents == amount_cents:
                        created = getattr(pay, "created_at", None) or ""
                        if created and str(appt_date) in str(created):
                            candidates.append(pay)
                    elif patient and _payment_relaxed_cross_device_match(pay, inv, patient):
                        candidates.append(pay)
                cursor = getattr(res, "cursor", None)
                if not cursor:
                    break
            if len(candidates) == 1:
                pid = getattr(candidates[0], "id", None)
                if pid and mark_invoice_paid_from_square(inv, pid):
                    logger.info(
                        "Reconciled invoice %s from sole customer payment on visit date %s (any device)",
                        inv.pk,
                        appt_date,
                    )
                    return True
        except Exception as exc:
            logger.debug("Square customer payment reconcile for invoice %s: %s", inv.pk, exc)

    try:
        begin = (inv.created_at or timezone.now()) - timedelta(days=7)
        begin_str = begin.strftime("%Y-%m-%dT%H:%M:%SZ")
        amount_filter = amount_cents if amount_cents >= _MIN_AMOUNT_CENTS else None
        cross_device_candidates: list = []
        for pay in _iter_square_payments(client, begin_str=begin_str, loc=loc, amount_cents=amount_filter):
            if _square_payment_matches_invoice(client, pay, inv):
                pid = getattr(pay, "id", None)
                if pid and mark_invoice_paid_from_square(inv, pid):
                    logger.info("Reconciled invoice %s from Square payment %s", inv.pk, pid)
                    return True
            if patient and _payment_relaxed_cross_device_match(pay, inv, patient):
                cross_device_candidates.append(pay)
        if patient and len(cross_device_candidates) == 1:
            pid = getattr(cross_device_candidates[0], "id", None)
            if pid and mark_invoice_paid_from_square(inv, pid):
                logger.info("Reconciled invoice %s from cross-device payment %s", inv.pk, pid)
                return True
        if amount_filter is not None:
            for pay in _iter_square_payments(client, begin_str=begin_str, loc=loc, amount_cents=None, max_pages=4):
                if _square_payment_matches_invoice(client, pay, inv):
                    pid = getattr(pay, "id", None)
                    if pid and mark_invoice_paid_from_square(inv, pid):
                        logger.info(
                            "Reconciled invoice %s from Square payment %s (note/order match, no amount filter)",
                            inv.pk,
                            pid,
                        )
                        return True
        if loc:
            for pay in _iter_square_payments(client, begin_str=begin_str, loc=None, amount_cents=amount_filter, max_pages=3):
                if _square_payment_matches_invoice(client, pay, inv):
                    pid = getattr(pay, "id", None)
                    if pid and mark_invoice_paid_from_square(inv, pid):
                        logger.info(
                            "Reconciled invoice %s from Square payment %s (no location filter)",
                            inv.pk,
                            pid,
                        )
                        return True
    except Exception as exc:
        logger.warning("Square payment list reconcile failed for invoice %s: %s", inv.pk, exc)

    inv.refresh_from_db()
    return inv.status == Invoice.Status.PAID


def _money_cents(invoice: Invoice) -> int:
    return int(Decimal(invoice.total_amount) * 100)


def build_credit_topup_reference(*, patient_id: int, amount_cents: int) -> str:
    nonce = uuid.uuid4().hex[:8]
    return f"{_CREDIT_TOPUP_REF_PREFIX}{int(patient_id)}_{int(amount_cents)}_{nonce}"[:40]


def parse_credit_topup_reference(ref: str | None) -> tuple[int, int] | None:
    if not ref:
        return None
    text = ref.strip()
    if not text.startswith(_CREDIT_TOPUP_REF_PREFIX):
        return None
    parts = text.split("_")
    if len(parts) != 4:
        return None
    try:
        patient_id = int(parts[1])
        amount_cents = int(parts[2])
        return patient_id, amount_cents
    except ValueError:
        return None


def apply_credit_topup_from_square_payment(
    *,
    square_payment_id: str,
    reference_id: str,
    amount_cents: int,
) -> bool:
    """
    Idempotently apply a completed Square payment as patient wallet credit.
    """
    parsed = parse_credit_topup_reference(reference_id)
    if not parsed:
        return False
    patient_id, expected_cents = parsed
    if amount_cents <= 0 or amount_cents != expected_cents:
        return False
    if PatientCreditTransaction.objects.filter(note=f"square_payment:{square_payment_id}").exists():
        return True

    with transaction.atomic():
        patient = Patient.objects.select_for_update().filter(pk=patient_id).first()
        if not patient:
            return False
        amount = (Decimal(amount_cents) / Decimal("100")).quantize(Decimal("0.01"))
        patient.credit_balance = (Decimal(patient.credit_balance or "0") + amount).quantize(Decimal("0.01"))
        patient.save(update_fields=["credit_balance", "updated_at"])
        PatientCreditTransaction.objects.create(
            patient=patient,
            kind=PatientCreditTransaction.Kind.TOP_UP,
            amount=amount,
            balance_after=patient.credit_balance,
            note=f"square_payment:{square_payment_id}",
            created_by=None,
        )
    return True


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


def _invoice_for_terminal_checkout(invoice: Invoice) -> Invoice:
    """Reload invoice with relations needed for Terminal display line items."""
    return (
        Invoice.objects.select_related("patient", "appointment", "appointment__booked_service", "visit")
        .prefetch_related("visit__rendered_services__service")
        .get(pk=invoice.pk)
    )


def _terminal_checkout_note(inv: Invoice) -> str:
    patient_name = f"{inv.patient.first_name or ''} {inv.patient.last_name or ''}".strip() or "Patient"
    appt = inv.appointment
    visit_date = str(appt.appointment_date) if appt else ""
    return f"{patient_name} · {visit_date} · {inv.invoice_number}"[:500]


def _build_terminal_order_line_items(inv: Invoice) -> list:
    """
    Line items shown on the Square Terminal when show_itemized_cart is enabled.
    First patient-charge line includes the client name; additional lines are service names.
    """
    from square.requests.money import MoneyParams
    from square.requests.order_line_item import OrderLineItemParams

    patient_name = f"{inv.patient.first_name or ''} {inv.patient.last_name or ''}".strip() or "Patient"
    items: list[OrderLineItemParams] = []
    visit = inv.visit
    if visit:
        charged = [rs for rs in visit.rendered_services.all() if rs.charges_patient]
        for idx, rs in enumerate(charged):
            svc_name = ((rs.service.name if rs.service else None) or "Service").strip()
            label = f"{patient_name} — {svc_name}" if idx == 0 else svc_name
            cents = int((Decimal(rs.total_price) * 100).quantize(Decimal("1")))
            if cents <= 0:
                continue
            items.append(
                OrderLineItemParams(
                    quantity=str(max(1, int(rs.quantity))),
                    name=label[:512],
                    item_type="ITEM",
                    base_price_money=MoneyParams(amount=cents, currency="USD"),
                )
            )
    if not items:
        svc = "Visit"
        if inv.appointment and inv.appointment.booked_service:
            svc = (inv.appointment.booked_service.name or "Visit").strip()
        amount_cents = _money_cents(inv)
        items.append(
            OrderLineItemParams(
                quantity="1",
                name=f"{patient_name} — {svc}"[:512],
                item_type="ITEM",
                base_price_money=MoneyParams(amount=amount_cents, currency="USD"),
            )
        )
    return items


def _create_square_order_for_terminal_checkout(inv: Invoice, *, amount_cents: int) -> str | None:
    """
    Create an OPEN Square order so the Terminal can show an itemized cart (name + services + amount).
    Returns order id or None if order creation fails (caller may fall back to amount-only checkout).
    """
    from square.requests.money import MoneyParams
    from square.requests.order import OrderParams
    from square.requests.order_line_item_discount import OrderLineItemDiscountParams

    loc = get_location_id()
    if not loc:
        return None

    line_items = _build_terminal_order_line_items(inv)
    if not line_items:
        return None

    lines_subtotal_cents = 0
    for li in line_items:
        bpm = getattr(li, "base_price_money", None)
        if bpm is None and isinstance(li, dict):
            bpm = li.get("base_price_money")
        qty = getattr(li, "quantity", None) or (li.get("quantity") if isinstance(li, dict) else "1")
        try:
            q = max(1, int(str(qty)))
        except ValueError:
            q = 1
        amt = getattr(bpm, "amount", None) if bpm is not None else (bpm or {}).get("amount")
        if isinstance(amt, int):
            lines_subtotal_cents += amt * q

    order_kwargs: dict = {
        "location_id": loc,
        "reference_id": str(inv.id)[:40],
        "line_items": line_items,
    }
    discount_cents = lines_subtotal_cents - amount_cents
    if discount_cents > 0:
        order_kwargs["discounts"] = [
            OrderLineItemDiscountParams(
                name="Professional discount",
                type="FIXED_AMOUNT",
                scope="ORDER",
                amount_money=MoneyParams(amount=discount_cents, currency="USD"),
            )
        ]
    elif lines_subtotal_cents != amount_cents:
        # Totals must match Terminal amount — one line: client name + primary service.
        from square.requests.order_line_item import OrderLineItemParams

        svc = "Visit"
        if inv.appointment and inv.appointment.booked_service:
            svc = (inv.appointment.booked_service.name or "Visit").strip()
        patient_name = f"{inv.patient.first_name or ''} {inv.patient.last_name or ''}".strip() or "Patient"
        order_kwargs["line_items"] = [
            OrderLineItemParams(
                quantity="1",
                name=f"{patient_name} — {svc}"[:512],
                item_type="ITEM",
                base_price_money=MoneyParams(amount=amount_cents, currency="USD"),
            )
        ]

    client = get_square_client()
    try:
        res = client.orders.create(
            idempotency_key=str(uuid.uuid4()),
            order=OrderParams(**order_kwargs),
        )
    except Exception as exc:
        logger.warning("Square order create for Terminal failed: %s", exc)
        return None
    if res.errors:
        logger.warning(
            "Square order create for Terminal failed: %s",
            getattr(res.errors[0], "detail", None) or res.errors,
        )
        return None
    order = res.order
    if order and order.id:
        return order.id
    return None


def _send_terminal_checkout(
    *,
    amount_cents: int,
    device_id: str,
    reference_id: str,
    note: str,
    invoice: Invoice | None = None,
) -> dict:
    """Create Terminal checkout; uses Square Order + itemized cart when invoice is provided."""
    from square.requests.device_checkout_options import DeviceCheckoutOptionsParams
    from square.requests.money import MoneyParams
    from square.requests.terminal_checkout import TerminalCheckoutParams

    order_id: str | None = None
    if invoice is not None:
        try:
            inv = _invoice_for_terminal_checkout(invoice)
            order_id = _create_square_order_for_terminal_checkout(inv, amount_cents=amount_cents)
        except Exception as exc:
            logger.warning("Terminal order prep failed, using amount-only checkout: %s", exc)

    device_options_kwargs: dict = {"device_id": device_id}
    if order_id:
        device_options_kwargs["show_itemized_cart"] = True

    client = get_square_client()
    checkout_kwargs: dict = {
        "amount_money": MoneyParams(amount=amount_cents, currency="USD"),
        "reference_id": reference_id[:40],
        "note": note[:500],
        "device_options": DeviceCheckoutOptionsParams(**device_options_kwargs),
        "payment_type": "CARD_PRESENT",
    }
    if order_id:
        checkout_kwargs["order_id"] = order_id

    res = client.terminal.checkouts.create(
        idempotency_key=str(uuid.uuid4()),
        checkout=TerminalCheckoutParams(**checkout_kwargs),
    )
    if res.errors:
        raise RuntimeError(res.errors[0].detail if res.errors else "Terminal checkout failed")
    co = res.checkout
    if not co or not co.id:
        raise RuntimeError("Square did not return a terminal checkout id.")
    out = {"checkout_id": co.id, "status": getattr(co, "status", None) or "PENDING"}
    if order_id:
        out["order_id"] = order_id
    return out


def create_payment_link_for_credit_topup(
    *,
    patient_id: int,
    amount_usd: Decimal,
    patient_label: str = "",
    success_url: str,
    cancel_url: str | None = None,
) -> tuple[str | None, str]:
    """
    Hosted Square checkout for wallet credit top-up.
    Returns (url, reference_id). reference_id encodes patient + cents for webhook verification.
    """
    from square.requests.money import MoneyParams
    from square.requests.order import OrderParams
    from square.requests.order_line_item import OrderLineItemParams

    loc = get_location_id()
    if not loc:
        return None, ""

    cents = int(Decimal(amount_usd) * 100)
    if cents < _MIN_AMOUNT_CENTS:
        return None, ""

    nonce = uuid.uuid4().hex[:8]
    reference_id = f"ct_{int(patient_id)}_{cents}_{nonce}"[:40]
    line_name = "Patient credit top-up"
    if patient_label.strip():
        line_name = f"Credit top-up — {patient_label.strip()}"[:120]

    client = get_square_client()
    order = OrderParams(
        location_id=loc,
        reference_id=reference_id,
        line_items=[
            OrderLineItemParams(
                quantity="1",
                name=line_name,
                item_type="ITEM",
                base_price_money=MoneyParams(amount=cents, currency="USD"),
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
        description=f"Patient credit top-up {patient_label}".strip()[:200],
        order=order,
        checkout_options=checkout_options,
    )
    if res.errors:
        logger.warning("Square credit top-up link error: %s", res.errors)
        return None, reference_id
    pl = res.payment_link
    return (pl.url if pl and pl.url else None), reference_id


def _terminal_bundle_note(primary: Invoice, extra_invoice_ids: list[int]) -> str:
    from .patient_payment_pending import combined_total_for_invoice_ids

    inv = _invoice_for_terminal_checkout(primary)
    base = _terminal_checkout_note(inv)
    if not extra_invoice_ids:
        return base
    extras = list(
        Invoice.objects.filter(pk__in=extra_invoice_ids)
        .exclude(pk=primary.pk)
        .order_by("created_at")
        .only("invoice_number", "kind")
    )
    if not extras:
        return base
    labels = ", ".join(f"{i.invoice_number}" for i in extras)
    total = combined_total_for_invoice_ids([primary.pk, *[e.pk for e in extras]])
    return f"{base} + prior fees ({labels}) · total ${total}"[:500]


def create_terminal_checkout_for_invoice(
    invoice: Invoice,
    *,
    include_pending_fees: bool = False,
) -> dict:
    """
    Send a card-present payment to the configured Square Terminal device.
    When include_pending_fees is True, charge visit + open no-show/late-cancel invoices.
    Returns {"checkout_id": str, "status": str, "bundle_invoice_ids": list[int]} or raises.
    """
    from .patient_payment_pending import combined_total_for_invoice_ids, invoice_ids_for_doctor_bundle

    device_id = get_terminal_device_id()
    if not device_id:
        raise ValueError("SQUARE_DEVICE_ID is not set — pair your Terminal in the Square Dashboard and paste the device id.")

    inv = _invoice_for_terminal_checkout(invoice)
    bundle_ids = invoice_ids_for_doctor_bundle(inv, include_pending_fees=include_pending_fees)
    amount_cents = int((combined_total_for_invoice_ids(bundle_ids) * 100).quantize(Decimal("1")))
    if amount_cents < _MIN_AMOUNT_CENTS:
        raise ValueError("Amount is below the minimum for card processing.")

    reference_id = bundle_reference_id(inv.pk) if len(bundle_ids) > 1 else str(inv.pk)
    extra_ids = [i for i in bundle_ids if i != inv.pk]
    out = _send_terminal_checkout(
        amount_cents=amount_cents,
        device_id=device_id,
        reference_id=reference_id,
        note=_terminal_bundle_note(inv, extra_ids),
        invoice=inv,
    )
    out["bundle_invoice_ids"] = bundle_ids
    out["amount_cents"] = amount_cents
    return out


def try_reconcile_bundle_terminal_payment(invoice: Invoice) -> bool:
    """If a bundle Terminal checkout completed, mark all bundled invoices paid."""
    from .patient_payment_pending import combined_total_for_invoice_ids, invoice_ids_for_doctor_bundle

    if not square_configured():
        return False
    ref = bundle_reference_id(invoice.pk)
    client = get_square_client()
    try:
        for co in _iter_terminal_checkouts(client, reference_ids=[ref]):
            if (getattr(co, "status", None) or "").upper() != "COMPLETED":
                continue
            payment_ids = getattr(co, "payment_ids", None) or []
            if not payment_ids:
                continue
            bundle_ids = invoice_ids_for_doctor_bundle(invoice, include_pending_fees=True)
            expected_cents = int((combined_total_for_invoice_ids(bundle_ids) * 100).quantize(Decimal("1")))
            am = getattr(co, "amount_money", None)
            paid_cents = int(getattr(am, "amount", 0) or 0) if am else 0
            if paid_cents and paid_cents < expected_cents:
                continue
            if mark_invoices_paid_from_square_bundle(bundle_ids, payment_ids[0]):
                logger.info("Reconciled bundle payment for invoices %s", bundle_ids)
                return True
    except Exception as exc:
        logger.debug("Bundle terminal reconcile for invoice %s: %s", invoice.pk, exc)
    return False


def try_push_terminal_checkout_to_kiosk(invoice: Invoice) -> str | None:
    """
    Best-effort: send amount due to the clinic kiosk Square Terminal (Terminal API checkout).

    Uses the same Create Terminal Checkout call as the desk reader with device id from
    get_kiosk_terminal_device_id(). Never raises — failures are logged only (bill is already saved).

    Returns Square checkout_id when queued so the doctor UI can poll until payment completes.
    """
    try:
        if not square_configured():
            return None

        device_id = get_kiosk_terminal_device_id()
        if not device_id:
            logger.debug("try_push_terminal_checkout_to_kiosk: no kiosk or fallback device id configured")
            return None

        inv = Invoice.objects.select_related("patient", "appointment", "visit").filter(pk=invoice.pk).first()
        if not inv or inv.status == Invoice.Status.PAID or inv.status == Invoice.Status.VOID:
            return None

        amount_cents = _money_cents(inv)
        if amount_cents < _MIN_AMOUNT_CENTS:
            logger.debug("try_push_terminal_checkout_to_kiosk: amount below minimum (%s cents)", amount_cents)
            return None

        out = _send_terminal_checkout(
            amount_cents=amount_cents,
            device_id=device_id,
            reference_id=str(inv.id),
            note=_terminal_checkout_note(inv),
            invoice=inv,
        )
        checkout_id = out.get("checkout_id")
        logger.info(
            "Kiosk Terminal checkout queued for invoice %s checkout_id=%s device=%s… order=%s",
            inv.id,
            checkout_id,
            device_id[:8],
            out.get("order_id"),
        )
        return checkout_id
    except Exception as exc:
        logger.warning(
            "Kiosk Terminal checkout failed after bill save (non-blocking): %s",
            exc,
            exc_info=True,
        )
        return None


def create_terminal_checkout_for_credit_topup(*, patient_id: int, amount_usd: Decimal, note: str = "") -> dict:
    """
    Send a card-present top-up charge to the configured Square Terminal.
    The webhook / poller applies credit by encoded reference_id.
    """
    device_id = get_terminal_device_id()
    if not device_id:
        raise ValueError("SQUARE_DEVICE_ID is not set — pair your Terminal in the Square Dashboard and paste the device id.")

    amount_cents = int(Decimal(amount_usd) * 100)
    if amount_cents < _MIN_AMOUNT_CENTS:
        raise ValueError("Amount is below the minimum for card processing.")

    reference_id = build_credit_topup_reference(patient_id=patient_id, amount_cents=amount_cents)
    out = _send_terminal_checkout(
        amount_cents=amount_cents,
        device_id=device_id,
        reference_id=reference_id,
        note=(note or f"Patient credit top-up ({patient_id})")[:500],
        invoice=None,
    )
    out["reference_id"] = reference_id
    return out


def create_terminal_checkout_test(amount_cents: int) -> dict:
    """
    Admin-only: charge a test amount on the configured Terminal without an invoice.

    Uses a non-numeric reference_id so polling never attaches this payment to an invoice.
    """
    device_id = get_terminal_device_id()
    if not device_id:
        raise ValueError("SQUARE_DEVICE_ID is not set — pair your Terminal in the Square Dashboard and paste the device id.")

    if amount_cents < _MIN_AMOUNT_CENTS:
        raise ValueError("Amount is below the minimum for card processing.")

    ref = f"admtest_{uuid.uuid4().hex}"[:40]
    return _send_terminal_checkout(
        amount_cents=amount_cents,
        device_id=device_id,
        reference_id=ref,
        note="Admin Terminal connectivity test",
        invoice=None,
    )


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
        inv_pk = invoice_id_from_square_reference(ref)
        if inv_pk:
            inv = invoice_open_for_square_settlement(inv_pk)
            if inv:
                if ref.startswith(_BUNDLE_REF_PREFIX):
                    from .patient_payment_pending import invoice_ids_for_doctor_bundle

                    bundle_ids = invoice_ids_for_doctor_bundle(inv, include_pending_fees=True)
                    mark_invoices_paid_from_square_bundle(bundle_ids, pid)
                    out["bundle_invoice_ids"] = bundle_ids
                else:
                    mark_invoice_paid_from_square(inv, pid)
        elif not ref.isdigit():
            parsed = parse_credit_topup_reference(ref)
            if parsed:
                _, expected_cents = parsed
                applied = apply_credit_topup_from_square_payment(
                    square_payment_id=pid,
                    reference_id=ref,
                    amount_cents=expected_cents,
                )
                out["credit_applied"] = bool(applied)
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
            "patient_credit_balance": str(invoice.patient.credit_balance),
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
            "patient_credit_balance": str(invoice.patient.credit_balance),
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
                "patient_credit_balance": str(invoice.patient.credit_balance),
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
        "patient_credit_balance": str(invoice.patient.credit_balance),
        "already_paid": False,
        "payment": payment,
    }
