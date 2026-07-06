"""Square customer + saved cards (Web Payments SDK token). See https://developer.squareup.com/docs/web-payments/overview"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

from django.conf import settings

# Used for admin “connection test” HTTP calls; bump occasionally to match Square’s supported versions.
_SQUARE_API_VERSION = "2024-11-20"

logger = logging.getLogger(__name__)


def square_configured() -> bool:
    return bool(os.environ.get("SQUARE_ACCESS_TOKEN", "").strip())


def _square_environment():
    from square.environment import SquareEnvironment

    raw = (getattr(settings, "SQUARE_ENVIRONMENT", None) or os.environ.get("SQUARE_ENVIRONMENT", "sandbox")).strip().lower()
    return SquareEnvironment.PRODUCTION if raw == "production" else SquareEnvironment.SANDBOX


def get_square_client():
    from square import Square

    token = os.environ.get("SQUARE_ACCESS_TOKEN", "").strip()
    if not token:
        raise RuntimeError("SQUARE_ACCESS_TOKEN is not set.")
    return Square(environment=_square_environment(), token=token)


def get_application_id() -> str:
    return os.environ.get("SQUARE_APPLICATION_ID", "").strip()


def get_location_id() -> str:
    return os.environ.get("SQUARE_LOCATION_ID", "").strip()


def square_web_sdk_environment() -> str:
    """
    Environment for the Web Payments SDK script (sandbox vs production).
    The application id prefix is authoritative — the SDK script must match the app id
    used to initialize payments(), not only SQUARE_ENVIRONMENT.
    """
    app_id = get_application_id()
    if app_id.startswith("sandbox-"):
        return "sandbox"
    if app_id:
        return "production"
    configured = (
        getattr(settings, "SQUARE_ENVIRONMENT", None) or os.environ.get("SQUARE_ENVIRONMENT", "sandbox") or "sandbox"
    ).strip().lower()
    return "production" if configured == "production" else "sandbox"


def square_environment_mismatch_warning() -> str | None:
    """Staff-facing hint when application id and access token environments disagree."""
    app_id = get_application_id()
    token_env = (
        getattr(settings, "SQUARE_ENVIRONMENT", None) or os.environ.get("SQUARE_ENVIRONMENT", "sandbox") or "sandbox"
    ).strip().lower()
    if app_id.startswith("sandbox-") and token_env == "production":
        return (
            "Square is misconfigured: the application ID is sandbox but SQUARE_ENVIRONMENT is production. "
            "Set SQUARE_ENVIRONMENT=sandbox, or switch to production application ID and access token."
        )
    if app_id and not app_id.startswith("sandbox-") and token_env == "sandbox":
        return (
            "Square is misconfigured: the application ID looks like production but SQUARE_ENVIRONMENT is sandbox. "
            "Use matching sandbox or production credentials in Admin → Settings."
        )
    return None


def assert_square_save_card_ready() -> None:
    """Raise RuntimeError when Square cannot safely save a Web Payments card token."""
    if not get_application_id() or not get_location_id():
        raise RuntimeError(
            "Square application ID or location ID is missing. Open Admin → Settings and connect Square."
        )
    warn = square_environment_mismatch_warning()
    if warn:
        raise RuntimeError(warn)


def get_terminal_device_id() -> str:
    """Paired Square Terminal device id (from Developer Dashboard or Devices API)."""
    return os.environ.get("SQUARE_DEVICE_ID", "").strip()


def get_kiosk_terminal_device_id() -> str:
    """
    Square Terminal device id for the patient-facing kiosk display/checkout.
    When unset, falls back to SQUARE_DEVICE_ID so single-terminal clinics still work.
    """
    raw = (os.environ.get("SQUARE_KIOSK_DEVICE_ID") or getattr(settings, "SQUARE_KIOSK_DEVICE_ID", "") or "").strip()
    if raw:
        return raw
    return get_terminal_device_id()


def _square_api_host() -> str:
    env_raw = (
        getattr(settings, "SQUARE_ENVIRONMENT", None) or os.environ.get("SQUARE_ENVIRONMENT", "sandbox") or "sandbox"
    ).strip().lower()
    return "https://connect.squareup.com" if env_raw == "production" else "https://connect.squareupsandbox.com"


def square_api_json(method: str, path: str, body: dict | None = None) -> dict:
    """Low-level Square REST call (same API version as admin health check)."""
    token = (os.environ.get("SQUARE_ACCESS_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("SQUARE_ACCESS_TOKEN is not set.")
    url = f"{_square_api_host()}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Square-Version": _SQUARE_API_VERSION,
            "Content-Type": "application/json",
        },
        method=method.upper(),
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode())


def square_search_orders(
    *,
    location_ids: list[str],
    reference_ids: list[str] | None = None,
    customer_ids: list[str] | None = None,
) -> list[dict]:
    """Completed Square orders — used to find payments when reference_id is on the order, not the payment."""
    if not location_ids:
        return []
    filt: dict = {"state_filter": {"states": ["COMPLETED"]}}
    if reference_ids:
        filt["reference_id_filter"] = {"reference_ids": [str(r)[:40] for r in reference_ids[:10]]}
    if customer_ids:
        filt["customer_filter"] = {"customer_ids": [str(c) for c in customer_ids[:1]]}
    payload = {"location_ids": location_ids, "query": {"filter": filt}}
    try:
        res = square_api_json("POST", "/v2/orders/search", payload)
    except Exception as exc:
        logging.getLogger(__name__).debug("square_search_orders failed: %s", exc)
        return []
    return res.get("orders") or []


def _square_locations_http_ping() -> tuple[bool, str | None, set[str]]:
    """
    Call Square ListLocations with the configured token (admin health check).
    Returns (success, error_message_or_none, location_ids_from_account).
    """
    token = (os.environ.get("SQUARE_ACCESS_TOKEN") or "").strip()
    if not token:
        return False, "SQUARE_ACCESS_TOKEN is not set.", set()
    env_raw = (
        getattr(settings, "SQUARE_ENVIRONMENT", None) or os.environ.get("SQUARE_ENVIRONMENT", "sandbox") or "sandbox"
    ).strip().lower()
    host = "https://connect.squareup.com" if env_raw == "production" else "https://connect.squareupsandbox.com"
    req = urllib.request.Request(
        f"{host}/v2/locations",
        headers={
            "Authorization": f"Bearer {token}",
            "Square-Version": _SQUARE_API_VERSION,
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode()
            body = json.loads(raw)
            errs = body.get("errors") or []
            if errs:
                return False, str(errs[0].get("detail") or errs[0].get("code") or errs[0])[:400], set()
        except Exception:
            pass
        return False, f"Square returned HTTP {exc.code}. Check the token and environment (sandbox vs production).", set()
    except Exception as exc:
        return False, str(exc)[:400], set()

    errs = payload.get("errors") or []
    if errs:
        return False, str(errs[0].get("detail") or errs[0])[:400], set()
    locs = payload.get("locations") or []
    ids = {loc["id"] for loc in locs if isinstance(loc, dict) and loc.get("id")}
    return True, None, ids


def get_square_payment_status_for_admin() -> dict:
    """
    Safe summary for Admin Settings (no secrets). Optionally pings Square ListLocations.
    """
    token_set = bool((os.environ.get("SQUARE_ACCESS_TOKEN") or "").strip())
    app_set = bool(get_application_id())
    loc_set = bool(get_location_id())
    env_raw = (
        getattr(settings, "SQUARE_ENVIRONMENT", None) or os.environ.get("SQUARE_ENVIRONMENT", "sandbox") or "sandbox"
    ).strip().lower()
    device_set = bool(get_terminal_device_id())
    webhook_key_set = bool((getattr(settings, "SQUARE_WEBHOOK_SIGNATURE_KEY", "") or "").strip())
    webhook_url_set = bool((getattr(settings, "SQUARE_WEBHOOK_NOTIFICATION_URL", "") or "").strip())
    frontend_set = bool((getattr(settings, "FRONTEND_BASE_URL", "") or "").strip())
    pos_cb_set = bool((getattr(settings, "SQUARE_POS_CALLBACK_URL", "") or "").strip())

    web_ready = token_set and app_set and loc_set
    terminal_ready = web_ready and device_set

    api_ok: bool | None = None
    api_message: str | None = None
    location_matches: bool | None = None
    location_ids_count = 0

    if token_set:
        ok, msg, ids = _square_locations_http_ping()
        api_ok = ok
        api_message = msg
        location_ids_count = len(ids)
        cfg_loc = get_location_id()
        if ok and cfg_loc:
            location_matches = cfg_loc in ids
        elif ok and not cfg_loc:
            location_matches = None

    summary = "Payments are not configured (no Square access token on the server)."
    if token_set:
        if api_ok is False:
            summary = "Square rejected the connection — check the access token and sandbox/production mode."
        elif not web_ready:
            summary = "Token works with Square, but Web Payments needs Application ID and Location ID in server settings."
        elif location_matches is False:
            summary = "Square is reachable, but your Location ID does not match this account — update SQUARE_LOCATION_ID."
        elif api_ok:
            summary = "Square connection looks good for online card save and payment links."
            if not terminal_ready:
                summary += " Add a Terminal device id if you use the card reader at the desk."

    return {
        "environment": env_raw if token_set else None,
        "summary": summary,
        "checks": [
            {
                "id": "access_token",
                "label": "Square access token (server)",
                "ok": token_set,
                "hint": "Set SQUARE_ACCESS_TOKEN in the API environment (see README).",
            },
            {
                "id": "application_id",
                "label": "Application ID (booking page card form)",
                "ok": app_set,
                "hint": "SQUARE_APPLICATION_ID — from Square Developer Dashboard.",
            },
            {
                "id": "location_id",
                "label": "Location ID (charges & checkout)",
                "ok": loc_set,
                "hint": "SQUARE_LOCATION_ID — must belong to the same Square account as the token.",
            },
            {
                "id": "api_live_test",
                "label": "Live test: call Square API",
                "ok": True if api_ok is True else (False if api_ok is False else None),
                "hint": api_message or ("Skipped — no token." if not token_set else None),
            },
            {
                "id": "location_match",
                "label": "Location ID matches your Square account",
                "ok": True if location_matches is True else (False if location_matches is False else None),
                "hint": None
                if location_matches is not False
                else "Copy the correct location id from Square Dashboard → Locations.",
            },
            {
                "id": "terminal_device",
                "label": "Terminal device id (optional — card reader)",
                "ok": True if device_set else None,
                "hint": None if device_set else "SQUARE_DEVICE_ID — only if doctors use “Use card reader”.",
            },
            {
                "id": "webhook",
                "label": "Webhook signature key (optional — auto-mark paid)",
                "ok": True if webhook_key_set else None,
                "hint": None if webhook_key_set else "SQUARE_WEBHOOK_SIGNATURE_KEY — for POST /api/v1/square/webhook/",
            },
            {
                "id": "frontend_url",
                "label": "Frontend base URL (payment link return)",
                "ok": True if frontend_set else None,
                "hint": None if frontend_set else "FRONTEND_BASE_URL — where patients return after paying a link.",
            },
            {
                "id": "pos_callback",
                "label": "Square POS callback URL (iPad/Android Square POS app)",
                "ok": True if pos_cb_set else None,
                "hint": None
                if pos_cb_set
                else "SQUARE_POS_CALLBACK_URL — optional; not a device id. Used if launching Square POS from a tablet browser.",
            },
        ],
        "web_payments_ready": web_ready and api_ok is True and location_matches is not False,
        "terminal_reader_ready": terminal_ready and api_ok is True and location_matches is not False,
        "pos_callback_configured": pos_cb_set,
        "square_locations_found": location_ids_count,
    }


def patient_has_chargeable_saved_card(patient) -> bool:
    """True when Square has both customer + card ids needed for card-not-present charges."""
    return bool(
        (getattr(patient, "square_customer_id", "") or "").strip()
        and (getattr(patient, "square_card_id", "") or "").strip()
    )


def patient_saved_card_display(patient) -> dict:
    """
    Card-on-file hints for UI.
    card_last4 may exist without chargeable Square ids (legacy/partial save) — flag that case.
    """
    last4 = (getattr(patient, "card_last4", "") or "").strip()
    brand = (getattr(patient, "card_brand", "") or "").strip()
    card_id = (getattr(patient, "square_card_id", "") or "").strip()
    chargeable = patient_has_chargeable_saved_card(patient)
    return {
        "card_brand": brand,
        "card_last4": last4,
        # Display: card on file when Square card id + last4 exist (legacy rows may lack customer_id).
        "has_saved_card": bool(card_id and last4),
        # Broader UI hint — show card section when digits or Square card id exist on the profile.
        "has_card_on_file": bool(last4 or card_id),
        "has_chargeable_saved_card": chargeable,
        "card_display_only": bool(last4 and not chargeable),
    }


def _square_error_field(err, field: str) -> str:
    if err is None:
        return ""
    if isinstance(err, dict):
        return (err.get(field) or "").strip()
    return (getattr(err, field, None) or "").strip()


def square_error_list_message(errors, *, status_code: int | None = None) -> str:
    """Human-readable message from Square errors[] (objects or dicts)."""
    if not errors:
        if status_code == 401:
            return "Square access token is invalid or expired. Check Admin → Settings."
        if status_code == 403:
            return "Square rejected this request (permission denied). Check the token and sandbox vs production."
        return "Square request failed."
    err = errors[0]
    code = _square_error_field(err, "code")
    detail = _square_error_field(err, "detail")
    if code and detail and code not in detail:
        return f"{detail} ({code})"
    return detail or code or "Square request failed."


def looks_like_technical_square_error(text: str) -> bool:
    """True when str(exception) is HTTP metadata, not a staff-facing message."""
    t = (text or "").strip().lower()
    if not t:
        return False
    if "headers" in t and ("square-version" in t or "content-type" in t or "cf-ray" in t):
        return True
    if "'server':" in t and "cloudflare" in t:
        return True
    if t.startswith("status_code=") or t.startswith("http "):
        return True
    return False


def format_square_exception(exc: BaseException) -> str:
    """
    Turn a Square SDK ApiError (or similar) into a short staff-facing message.
    Never returns raw HTTP headers.
    """
    errors = getattr(exc, "errors", None)
    status_code = getattr(exc, "status_code", None)
    if errors:
        return square_error_list_message(errors, status_code=status_code)[:500]

    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        msg = square_error_list_message(body.get("errors") or [], status_code=status_code)
        if msg != "Square request failed.":
            return msg[:500]
    elif isinstance(body, str) and body.strip():
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                msg = square_error_list_message(parsed.get("errors") or [], status_code=status_code)
                if msg != "Square request failed.":
                    return msg[:500]
        except json.JSONDecodeError:
            pass

    if status_code == 401:
        return "Square access token is invalid or expired. Check Admin → Settings."
    if status_code == 403:
        return "Square rejected this charge (permission denied). Check Admin → Settings."
    if status_code == 404:
        return "Square cannot find this saved card. Re-save the card on the patient chart (sandbox vs production mismatch is common)."

    text = str(exc).strip()
    if looks_like_technical_square_error(text):
        return (
            "Square could not process this charge. Check Admin → Settings (sandbox vs production), "
            "re-save the patient's card, or use cash/Terminal."
        )
    return text[:500] if text else "Square could not process this charge."


def format_save_card_exception(exc: BaseException) -> str:
    """Staff-facing message when saving a card on file fails — never raw HTTP headers."""
    msg = format_square_exception(exc)
    upper = msg.upper()
    if "INVALID_CARD_DATA" in upper:
        hint = square_environment_mismatch_warning()
        if hint:
            return hint
        if square_web_sdk_environment() == "sandbox":
            return (
                "Square could not save this card (invalid card data). "
                "In sandbox, use test card 4111 1111 1111 1111 with any future expiry and CVV, "
                "then tap Save card securely right away."
            )
        return (
            "Square could not save this card (invalid card data). "
            "Ask the patient to re-enter the card and save immediately. "
            "If this continues, check Admin → Settings that sandbox vs production credentials match."
        )
    if looks_like_technical_square_error(msg):
        hint = square_environment_mismatch_warning()
        if hint:
            return hint
        return (
            "Square could not save this card. Check Admin → Settings (sandbox vs production), "
            "then try again with a fresh card entry."
        )
    return msg


def _square_api_error_message(errors) -> str:
    return square_error_list_message(errors)


def _persist_patient_square_card(patient, card) -> None:
    """Update patient row from a Square Card object."""
    last4 = getattr(card, "last_4", None) or getattr(card, "last4", None)
    patient.square_card_id = card.id
    patient.card_brand = (getattr(card, "card_brand", None) or "") or ""
    patient.card_last4 = (str(last4) if last4 else "")[-4:] if last4 else ""
    cid = (getattr(card, "customer_id", None) or patient.square_customer_id or "").strip()
    if cid:
        patient.square_customer_id = cid
    patient.save(
        update_fields=[
            "square_customer_id",
            "square_card_id",
            "card_brand",
            "card_last4",
            "updated_at",
        ]
    )


def resolve_chargeable_card_for_patient(patient) -> tuple[str | None, str | None, str | None]:
    """
    Verify the saved card still exists in Square before charging.
    Returns (card_id, customer_id, error_code).
    Refreshes stale card ids on the patient when Square has a newer enabled card.
    """
    if not square_configured():
        return None, None, "square_not_configured"

    cid = (getattr(patient, "square_customer_id", "") or "").strip()
    card_id = (getattr(patient, "square_card_id", "") or "").strip()
    client = get_square_client()

    def _card_usable(card) -> bool:
        if not card or not getattr(card, "id", None):
            return False
        if getattr(card, "enabled", True) is False:
            return False
        return True

    if card_id:
        try:
            res = client.cards.get(card_id=card_id)
            if res.errors:
                logger.info(
                    "Square card lookup failed for patient %s card %s: %s",
                    patient.pk,
                    card_id,
                    _square_api_error_message(res.errors),
                )
            elif _card_usable(res.card):
                _persist_patient_square_card(patient, res.card)
                return patient.square_card_id, patient.square_customer_id, None
        except Exception as exc:
            logger.warning("Square card get failed for patient %s: %s", patient.pk, exc)

    if cid:
        try:
            pager = client.cards.list(customer_id=cid, include_disabled=False)
            for card in pager:
                if _card_usable(card):
                    _persist_patient_square_card(patient, card)
                    return patient.square_card_id, patient.square_customer_id, None
        except Exception as exc:
            logger.warning("Square card list failed for patient %s: %s", patient.pk, exc)

    if (getattr(patient, "card_last4", "") or "").strip():
        return None, None, "card_on_file_not_chargeable"
    return None, None, "no_saved_card"


def ensure_square_customer(patient):
    """Create Square customer if missing; persist square_customer_id on patient."""
    client = get_square_client()
    if patient.square_customer_id:
        return patient.square_customer_id
    import uuid

    kwargs = {
        "idempotency_key": str(uuid.uuid4()),
        "given_name": (patient.first_name or "")[:100] or "Patient",
        "family_name": (patient.last_name or "")[:100] or ".",
        "reference_id": f"patient_{patient.id}"[:40],
        "note": f"ChiroFlow patient id {patient.id}",
    }
    if patient.phone and patient.phone.strip():
        kwargs["phone_number"] = patient.phone.strip()
    if patient.email and str(patient.email).strip():
        kwargs["email_address"] = str(patient.email).strip()
    res = client.customers.create(**kwargs)
    errs = getattr(res, "errors", None) or []
    if errs:
        raise RuntimeError(getattr(errs[0], "detail", None) or str(errs[0]) or "Square customer create failed")
    cust = res.customer
    if not cust or not cust.id:
        raise RuntimeError("Square did not return a customer id.")
    patient.square_customer_id = cust.id
    patient.save(update_fields=["square_customer_id", "updated_at"])
    return cust.id


def save_card_from_source(patient, source_id: str, verification_token: str | None = None) -> None:
    """Attach a card to the customer using a Web Payments token (source_id)."""
    import uuid

    from square.requests.card import CardParams

    assert_square_save_card_ready()
    token = (source_id or "").strip()
    if not token:
        raise RuntimeError("Card token was missing. Enter the card again and tap Save card securely.")

    client = get_square_client()
    cid = ensure_square_customer(patient)
    kwargs = {
        "idempotency_key": str(uuid.uuid4()),
        "source_id": token,
        "card": CardParams(customer_id=cid),
    }
    if verification_token:
        kwargs["verification_token"] = verification_token
    try:
        res = client.cards.create(**kwargs)
    except Exception as exc:
        logger.warning("Square cards.create failed for patient %s: %s", patient.pk, exc)
        raise RuntimeError(format_save_card_exception(exc)) from exc
    errs = getattr(res, "errors", None) or []
    if errs:
        raise RuntimeError(square_error_list_message(errs))
    card = res.card
    if not card or not card.id:
        raise RuntimeError("Square did not return a card id.")
    patient.square_card_id = card.id
    patient.card_brand = (getattr(card, "card_brand", None) or "") or ""
    last4 = getattr(card, "last_4", None) or getattr(card, "last4", None)
    patient.card_last4 = (str(last4) if last4 else "")[-4:] if last4 else ""
    patient.save(
        update_fields=[
            "square_customer_id",
            "square_card_id",
            "card_brand",
            "card_last4",
            "updated_at",
        ]
    )
