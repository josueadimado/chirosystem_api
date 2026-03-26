"""Square Point of Sale API web callback — no CSRF (Square POSTs from POS app)."""

from __future__ import annotations

import json
import logging

from django.conf import settings
from django.http import HttpResponse, HttpResponseRedirect
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import Invoice
from .square_pos import complete_invoice_from_pos_transaction, unsign_invoice_for_pos

logger = logging.getLogger(__name__)


def _frontend_base() -> str:
    return str(getattr(settings, "FRONTEND_BASE_URL", None) or "http://localhost:3001").rstrip("/")


def _parse_callback_payload(request) -> dict:
    """Square iOS returns JSON in `data`; Android uses com.squareup.pos.* form keys."""
    raw = request.GET.get("data") or request.POST.get("data")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Square POS callback: invalid JSON in data")

    tx = request.POST.get("com.squareup.pos.SERVER_TRANSACTION_ID") or request.GET.get(
        "com.squareup.pos.SERVER_TRANSACTION_ID"
    )
    err = request.POST.get("com.squareup.pos.ERROR_CODE") or request.GET.get("com.squareup.pos.ERROR_CODE")
    meta_raw = request.POST.get("com.squareup.pos.REQUEST_METADATA") or request.GET.get(
        "com.squareup.pos.REQUEST_METADATA"
    )
    out: dict = {}
    if tx:
        out["transaction_id"] = tx
    if err:
        out["error_code"] = err
        out["status"] = "error"
    if meta_raw:
        try:
            m = json.loads(meta_raw)
            if isinstance(m, dict) and m.get("state"):
                out["state"] = m["state"]
        except json.JSONDecodeError:
            pass
    return out


@csrf_exempt
@require_http_methods(["GET", "POST", "HEAD"])
def square_pos_callback(request):
    """
    Register this exact URL in Square Developer Console (POS API web callback).
    After payment on Square POS + reader, the browser returns here.
    """
    if request.method == "HEAD":
        return HttpResponse(status=200)

    payload = _parse_callback_payload(request)
    base = _frontend_base()

    err_code = (payload.get("error_code") or "").strip()
    st = (payload.get("status") or "").strip().lower()
    if err_code or st == "error":
        reason = err_code or "payment_canceled"
        return HttpResponseRedirect(f"{base}/doctor/dashboard?square_pos=err&reason={reason[:80]}")

    transaction_id = (payload.get("transaction_id") or "").strip()
    state = (payload.get("state") or "").strip()

    if not transaction_id or not state:
        logger.warning("Square POS callback missing transaction_id or state: %s", payload)
        return HttpResponseRedirect(f"{base}/doctor/dashboard?square_pos=err&reason=missing_data")

    try:
        invoice_id = unsign_invoice_for_pos(state)
    except Exception as exc:
        logger.warning("Square POS callback: bad state token: %s", exc)
        return HttpResponseRedirect(f"{base}/doctor/dashboard?square_pos=err&reason=invalid_state")

    inv = Invoice.objects.select_related("appointment").filter(pk=invoice_id).first()
    if not inv:
        return HttpResponseRedirect(f"{base}/doctor/dashboard?square_pos=err&reason=no_invoice")

    if inv.status != Invoice.Status.ISSUED:
        return HttpResponseRedirect(f"{base}/doctor/dashboard?square_pos=ok")

    ok, msg = complete_invoice_from_pos_transaction(invoice=inv, transaction_id=transaction_id)
    if not ok:
        logger.warning("Square POS complete failed: %s", msg)
        return HttpResponseRedirect(f"{base}/doctor/dashboard?square_pos=err&reason=record_failed")

    return HttpResponseRedirect(f"{base}/doctor/dashboard?square_pos=ok")
