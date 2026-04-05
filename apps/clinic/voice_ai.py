"""
Turn caller speech into structured booking fields using OpenAI (optional).

If OPENAI_API_KEY is not set, voice webhooks fall back to a helpful message.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from django.conf import settings

from .models import Service

logger = logging.getLogger(__name__)


def _booking_catalog_json() -> dict[str, Any]:
    """Same shape as /api/v1/booking-options/ (minimal fields for the model)."""
    bookable = Service.objects.filter(is_active=True, show_in_public_booking=True).order_by("name")
    services = []
    providers_by_service: dict[int, list[dict]] = {}
    for svc in bookable:
        services.append(
            {
                "id": svc.id,
                "name": svc.name,
                "duration_minutes": svc.duration_minutes,
                "price": str(svc.price),
                "service_type": svc.service_type,
                "allow_provider_choice": svc.service_type == "massage",
            }
        )
        providers_by_service[svc.id] = [
            {"id": p.id, "provider_name": str(p)}
            for p in svc.providers.filter(active=True)
        ]
    return {"services": services, "providers_by_service": providers_by_service}


def openai_extract_field(transcript: str, *, field: str, instruction: str) -> dict[str, Any] | None:
    """Lightweight OpenAI call to extract a single field from speech. Returns parsed JSON dict or None."""
    key = getattr(settings, "OPENAI_API_KEY", "") or ""
    if not key.strip():
        return None

    body = json.dumps(
        {
            "model": getattr(settings, "OPENAI_VOICE_MODEL", "gpt-4o-mini"),
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": transcript},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        content = raw["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as e:
        logger.warning("OpenAI extract_field (%s) failed: %s", field, e)
        return None


def openai_parse_booking_intent(*, transcript: str, today_iso: str, catalog: dict[str, Any]) -> dict[str, Any] | None:
    """Call OpenAI; return parsed JSON dict or None on failure."""
    key = getattr(settings, "OPENAI_API_KEY", "") or ""
    if not key.strip():
        return None

    system = (
        "You help parse phone booking requests for a chiropractic clinic. "
        "Return ONLY valid JSON with keys: "
        "first_name (string), last_name (string), "
        "service_id (integer or null), service_name_hint (string or null), "
        "provider_id (integer or null), provider_name_hint (string or null), "
        "appointment_date (YYYY-MM-DD), start_time (12-hour string like \"2:30 PM\" or \"9:00 AM\"), "
        "notes (string, optional). "
        "Use service_id from the catalog when the caller clearly matches one service; otherwise use service_name_hint. "
        "For massage (allow_provider_choice true), set provider_id if they name a therapist, else provider_name_hint. "
        "For chiropractic (allow_provider_choice false), provider_id may be null. "
        f"Today in the clinic calendar is {today_iso}. "
        "Interpret phrases like \"next Tuesday\" relative to that date. "
        "If something critical is missing, still return best-effort JSON with nulls."
    )
    user = json.dumps({"caller_said": transcript, "catalog": catalog}, default=str)

    body = json.dumps(
        {
            "model": getattr(settings, "OPENAI_VOICE_MODEL", "gpt-4o-mini"),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        logger.exception("OpenAI voice booking request failed: %s", e)
        return None

    try:
        content = raw["choices"][0]["message"]["content"]
        return json.loads(content)
    except (KeyError, IndexError, json.JSONDecodeError, TypeError) as e:
        logger.warning("OpenAI voice booking bad response: %s", e)
        return None


def _match_service(services: list[dict], service_id: int | None, name_hint: str | None) -> dict | None:
    if service_id is not None:
        for s in services:
            if s["id"] == int(service_id):
                return s
    if name_hint and str(name_hint).strip():
        hint = str(name_hint).lower().strip()
        best = None
        for s in services:
            n = s["name"].lower()
            if hint in n or n in hint:
                best = s
                break
        if best is None:
            for s in services:
                n = s["name"].lower()
                if any(word in n for word in hint.split() if len(word) > 3):
                    best = s
                    break
        return best
    return None


def _match_provider(
    providers: list[dict],
    provider_id: int | None,
    name_hint: str | None,
) -> int | None:
    if provider_id is not None:
        for p in providers:
            if p["id"] == int(provider_id):
                return int(provider_id)
    if name_hint and str(name_hint).strip():
        h = str(name_hint).lower()
        for p in providers:
            if h in p["provider_name"].lower():
                return int(p["id"])
    return None


def _parse_time_12h(s: str):
    s = (s or "").strip()
    for fmt in ("%I:%M %p", "%I %p", "%H:%M"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    m = re.match(r"^(\d{1,2}):(\d{2})\s*(am|pm)$", s.lower())
    if m:
        h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3)
        if ap == "pm" and h != 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        return datetime.strptime(f"{h}:{mi:02d}", "%H:%M").time()
    return None


def intent_to_booking_payload(
    intent: dict[str, Any],
    *,
    caller_e164: str,
    catalog: dict[str, Any],
) -> tuple[dict | None, str | None]:
    """
    Build PublicBookingSerializer payload from model output + catalog.
    Returns (payload, None) or (None, human-readable error).
    """
    services = catalog.get("services") or []
    if not services:
        return None, "No bookable services are configured."

    fn = (intent.get("first_name") or "").strip()
    ln = (intent.get("last_name") or "").strip()
    if not fn or not ln:
        return None, "missing_name"

    svc = _match_service(
        services,
        intent.get("service_id"),
        intent.get("service_name_hint"),
    )
    if not svc:
        return None, "missing_service"

    prov_id = None
    pbs = catalog.get("providers_by_service") or {}
    plist = pbs.get(svc["id"]) or []
    if svc.get("allow_provider_choice"):
        prov_id = _match_provider(plist, intent.get("provider_id"), intent.get("provider_name_hint"))
        if not prov_id and len(plist) == 1:
            prov_id = plist[0]["id"]
        if not prov_id:
            return None, "missing_provider"
    else:
        if plist:
            prov_id = plist[0]["id"]

    date_raw = intent.get("appointment_date") or ""
    try:
        appt_date = date.fromisoformat(str(date_raw)[:10])
    except ValueError:
        return None, "bad_date"

    t = _parse_time_12h(str(intent.get("start_time") or ""))
    if not t:
        return None, "bad_time"

    phone = caller_e164
    if phone.startswith("tel:"):
        phone = phone[4:]

    payload = {
        "first_name": fn[:100],
        "last_name": ln[:100],
        "phone": phone,
        "email": "",
        "service_id": svc["id"],
        "service_duration_minutes": int(svc["duration_minutes"]),
        "service_price": Decimal(str(svc["price"])),
        "appointment_date": appt_date,
        "start_time": t,
    }
    if prov_id is not None:
        payload["provider_id"] = int(prov_id)

    return payload, None
