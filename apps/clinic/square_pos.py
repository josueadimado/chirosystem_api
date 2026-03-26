"""
Square Point of Sale API (mobile web) — open Square POS on iPad/Android with an amount,
then complete payment on Stand + Reader. See:
https://developer.squareup.com/docs/pos-api/build-mobile-web
https://developer.squareup.com/docs/pos-api/payments-integration
"""

from __future__ import annotations

import json
import logging
import os
from urllib.parse import quote

from django.conf import settings
from django.core import signing

from .models import Invoice
from .square_helpers import get_application_id, get_location_id, get_square_client
from .square_payment import _money_cents, mark_invoice_paid_from_square

logger = logging.getLogger(__name__)

_POS_SALT = "chiroflow.square.pos.v1"


def get_pos_callback_url() -> str:
    """HTTPS URL registered in Square Developer Console → Point of Sale → Web callback URL."""
    raw = (getattr(settings, "SQUARE_POS_CALLBACK_URL", None) or "").strip()
    if not raw:
        raw = os.environ.get("SQUARE_POS_CALLBACK_URL", "").strip()
    if not raw:
        return ""
    return raw if raw.endswith("/") else raw + "/"


def pos_callback_configured() -> bool:
    return bool(get_pos_callback_url())


def sign_invoice_for_pos(invoice_id: int) -> str:
    return signing.dumps({"invoice_id": invoice_id}, salt=_POS_SALT)


def unsign_invoice_for_pos(token: str, *, max_age: int = 172800) -> int:
    data = signing.loads(token, salt=_POS_SALT, max_age=max_age)
    return int(data["invoice_id"])


def square_payment_id_from_pos_transaction_id(transaction_id: str) -> str | None:
    """
    POS returns a transaction_id. Square maps it to an Order id; tenders hold Payment id.
    """
    tid = (transaction_id or "").strip()
    if not tid:
        return None
    client = get_square_client()
    res = client.orders.get(order_id=tid)
    errs = getattr(res, "errors", None) or []
    if errs:
        logger.warning("Square orders.get failed for POS transaction %s: %s", tid, errs)
        return None
    order = res.order
    if not order:
        return None
    tenders = order.tenders or []
    for t in tenders:
        pid = getattr(t, "payment_id", None) or getattr(t, "id", None)
        if pid:
            return str(pid).strip()
    return None


def _payment_amount_cents(payment_id: str) -> int | None:
    client = get_square_client()
    res = client.payments.get(payment_id)
    errs = getattr(res, "errors", None) or []
    if errs:
        return None
    pay = res.payment
    if not pay:
        return None
    am = getattr(pay, "amount_money", None)
    if am is None:
        return None
    return int(getattr(am, "amount", None) or 0)


def verify_pos_payment_matches_invoice(payment_id: str, invoice: Invoice) -> bool:
    """Allow exact match or higher (tip on device)."""
    expected = _money_cents(invoice)
    got = _payment_amount_cents(payment_id)
    if got is None:
        return False
    return got >= expected


def complete_invoice_from_pos_transaction(*, invoice: Invoice, transaction_id: str) -> tuple[bool, str | None]:
    """
    Resolve Square payment id from POS transaction_id, verify amount, mark invoice paid.
    Returns (ok, error_message).
    """
    pid = square_payment_id_from_pos_transaction_id(transaction_id)
    if not pid:
        return False, "Could not resolve Square payment from this transaction. Try again or mark paid manually."

    if not verify_pos_payment_matches_invoice(pid, invoice):
        return False, "Payment amount does not match the invoice total."

    if mark_invoice_paid_from_square(invoice, pid):
        return True, None
    return False, "Could not record payment (invoice may already be paid)."


def build_ios_square_pos_url(invoice: Invoice) -> str:
    """square-commerce-v1:// URL for iPad Safari → Square POS app + reader."""
    callback_url = get_pos_callback_url()
    if not callback_url:
        raise ValueError("SQUARE_POS_CALLBACK_URL is not set — add it in .env and register the same URL in Square.")

    client_id = get_application_id()
    if not client_id:
        raise ValueError("SQUARE_APPLICATION_ID is not set.")

    loc = get_location_id()
    if not loc:
        raise ValueError("SQUARE_LOCATION_ID is not set.")

    amount = _money_cents(invoice)
    if amount < 100:
        raise ValueError("Amount is below the minimum for card processing.")

    state = sign_invoice_for_pos(invoice.pk)
    data = {
        "amount_money": {"amount": amount, "currency": "USD"},
        "callback_url": callback_url,
        "client_id": client_id,
        "version": "1.3",
        "location_id": loc,
        "notes": f"ChiroFlow invoice {invoice.invoice_number}",
        "state": state,
        "options": {
            "supported_tender_types": ["CREDIT_CARD"],
            "auto_return": True,
            "skip_receipt": False,
        },
    }
    payload = json.dumps(data, separators=(",", ":"))
    return "square-commerce-v1://payment/create?data=" + quote(payload, safe="")


def build_android_square_pos_intent(invoice: Invoice) -> str:
    """intent:#Intent;… for Android tablet browsers opening Square POS."""
    callback_url = get_pos_callback_url()
    if not callback_url:
        raise ValueError("SQUARE_POS_CALLBACK_URL is not set.")

    client_id = get_application_id()
    if not client_id:
        raise ValueError("SQUARE_APPLICATION_ID is not set.")

    loc = get_location_id()
    if not loc:
        raise ValueError("SQUARE_LOCATION_ID is not set.")

    amount = _money_cents(invoice)
    if amount < 100:
        raise ValueError("Amount is below the minimum for card processing.")

    state = sign_invoice_for_pos(invoice.pk)
    # REQUEST_METADATA round-trips in the POS response (see Square POS API docs).
    meta = json.dumps({"state": state}, separators=(",", ":"))
    parts = [
        "intent:#Intent;",
        "action=com.squareup.pos.action.CHARGE;",
        "package=com.squareup;",
        f"S.browser_fallback_url={quote(callback_url, safe='')};",
        f"S.com.squareup.pos.WEB_CALLBACK_URI={quote(callback_url, safe='')};",
        f"S.com.squareup.pos.CLIENT_ID={quote(client_id, safe='')};",
        "S.com.squareup.pos.API_VERSION=v2.0;",
        f"i.com.squareup.pos.TOTAL_AMOUNT={amount};",
        "S.com.squareup.pos.CURRENCY_CODE=USD;",
        "S.com.squareup.pos.TENDER_TYPES=com.squareup.pos.TENDER_CARD;",
        f"S.com.squareup.pos.LOCATION_ID={quote(loc, safe='')};",
        f"S.com.squareup.pos.REQUEST_METADATA={quote(meta, safe='')};",
        f"S.com.squareup.pos.NOTE={quote(f'ChiroFlow invoice {invoice.invoice_number}', safe='')};",
        "end",
    ]
    return "".join(parts)
