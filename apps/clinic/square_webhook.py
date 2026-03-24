"""Square webhooks: payment.updated / terminal.checkout.updated → mark invoice paid."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os

from django.core.cache import cache
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import Invoice
from .square_payment import mark_invoice_paid_from_square

logger = logging.getLogger(__name__)


def _verify_square_signature(*, body: bytes, signature_header: str, signature_key: str, notification_url: str) -> bool:
    """https://developer.squareup.com/docs/webhooks/step3validate (notification URL + raw body string)."""
    if not signature_header or not signature_key:
        return False
    try:
        body_str = body.decode("utf-8")
    except UnicodeDecodeError:
        return False
    payload = (notification_url + body_str).encode("utf-8")
    mac = hmac.new(signature_key.encode("utf-8"), payload, hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode("utf-8")
    for raw in signature_header.split(","):
        part = raw.strip()
        if ":" in part:
            part = part.split(":", 1)[1].strip()
        if part and hmac.compare_digest(expected, part):
            return True
    return False


def _invoice_id_from_reference(ref: str | None) -> int | None:
    if not ref:
        return None
    ref = ref.strip()
    if ref.isdigit():
        try:
            return int(ref)
        except ValueError:
            return None
    return None


def _handle_payment_object(payment: dict) -> None:
    if not payment:
        return
    if (payment.get("status") or "").upper() != "COMPLETED":
        return
    pid = payment.get("id")
    if not pid:
        return
    inv_id = _invoice_id_from_reference(payment.get("reference_id"))
    if not inv_id:
        return
    inv = Invoice.objects.filter(pk=inv_id, status=Invoice.Status.ISSUED).first()
    if inv:
        mark_invoice_paid_from_square(inv, pid)


def _handle_terminal_checkout_object(checkout: dict) -> None:
    if not checkout:
        return
    if (checkout.get("status") or "").upper() != "COMPLETED":
        return
    payment_ids = checkout.get("payment_ids") or []
    if not payment_ids:
        return
    pid = payment_ids[0]
    inv_id = _invoice_id_from_reference(checkout.get("reference_id"))
    if not inv_id:
        return
    inv = Invoice.objects.filter(pk=inv_id, status=Invoice.Status.ISSUED).first()
    if inv:
        mark_invoice_paid_from_square(inv, pid)


@csrf_exempt
@require_POST
def square_webhook(request):
    signature_key = os.environ.get("SQUARE_WEBHOOK_SIGNATURE_KEY", "").strip()
    notification_url = os.environ.get("SQUARE_WEBHOOK_NOTIFICATION_URL", "").strip()
    if not signature_key:
        logger.error("SQUARE_WEBHOOK_SIGNATURE_KEY not configured")
        return HttpResponse("Webhook not configured", status=500)
    if not notification_url:
        notification_url = request.build_absolute_uri()

    sig = request.META.get("HTTP_X_SQUARE_HMACSHA256_SIGNATURE", "")
    body = request.body
    if not _verify_square_signature(
        body=body,
        signature_header=sig,
        signature_key=signature_key,
        notification_url=notification_url,
    ):
        return HttpResponse(status=400)

    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return HttpResponse(status=400)

    event_id = (payload.get("event_id") or "").strip()
    dedupe_key = f"square_webhook_evt:{event_id}" if event_id else None
    if dedupe_key and not cache.add(dedupe_key, 1, timeout=86400 * 7):
        return HttpResponse(status=200)

    try:
        event_type = payload.get("type") or payload.get("event_type") or ""
        data = payload.get("data") or {}
        obj = data.get("object") or {}

        if event_type.startswith("payment."):
            pay = obj.get("payment") or obj
            if isinstance(pay, dict) and "id" in pay and "status" in pay:
                _handle_payment_object(pay)
            elif isinstance(obj, dict) and obj.get("id") and obj.get("status"):
                _handle_payment_object(obj)

        elif event_type.startswith("terminal.checkout"):
            co = obj.get("checkout") or obj
            if isinstance(co, dict) and co.get("status"):
                _handle_terminal_checkout_object(co)
    except Exception:
        if dedupe_key:
            cache.delete(dedupe_key)
        logger.exception("Square webhook handler error")
        return HttpResponse(status=500)

    return HttpResponse(status=200)
