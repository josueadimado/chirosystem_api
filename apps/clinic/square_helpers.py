"""Square customer + saved cards (Web Payments SDK token). See https://developer.squareup.com/docs/web-payments/overview"""

from __future__ import annotations

import os

from django.conf import settings


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


def get_terminal_device_id() -> str:
    """Paired Square Terminal device id (from Developer Dashboard or Devices API)."""
    return os.environ.get("SQUARE_DEVICE_ID", "").strip()


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

    client = get_square_client()
    cid = ensure_square_customer(patient)
    kwargs = {
        "idempotency_key": str(uuid.uuid4()),
        "source_id": source_id.strip(),
        "card": CardParams(customer_id=cid),
    }
    if verification_token:
        kwargs["verification_token"] = verification_token
    res = client.cards.create(**kwargs)
    errs = getattr(res, "errors", None) or []
    if errs:
        raise RuntimeError(getattr(errs[0], "detail", None) or str(errs[0]) or "Square card create failed")
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
